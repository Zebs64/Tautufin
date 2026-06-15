"""Capture des sessions de lecture et filtres de durée minimale.

Le polling de ``/Sessions`` est la source de vérité : chaque passage met à
jour les sessions suivies et accumule le temps réellement regardé (le temps
écoulé n'est compté que si la lecture n'était pas en pause — équivalent du
``paused_counter`` de Tautulli). Une session absente du polling est
finalisée. Le webhook (plugin Jellyfin Webhook) accélère la prise en compte
des pauses et des stops mais n'est pas requis.

Filtres à la finalisation (comportement Tautulli, condition OR) : la session
est enregistrée si AU MOINS UN des seuils actifs est atteint ; si les deux
sont à 0, tout est enregistré.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import database
from .database import now_iso

logger = logging.getLogger(__name__)

TICKS_PER_SECOND = 10_000_000  # Jellyfin exprime les durées en ticks de 100 ns


def resolution_label(width, height) -> str | None:
    if not width and not height:
        return None
    w, h = int(width or 0), int(height or 0)
    if w >= 3600 or h >= 2000:
        return "4K"
    if w >= 1800 or h >= 1000:
        return "1080p"
    if w >= 1200 or h >= 700:
        return "720p"
    return "SD"


def should_record(play_duration: int, runtime_seconds: int | None,
                  minimum_duration: int, minimum_percent: int) -> tuple[bool, str]:
    """Applique les seuils, retourne (enregistrer, raison)."""
    if minimum_duration <= 0 and minimum_percent <= 0:
        return True, "filtres désactivés"
    if minimum_duration > 0 and play_duration >= minimum_duration:
        return True, f"durée {play_duration}s >= {minimum_duration}s"
    if minimum_percent > 0 and runtime_seconds:
        percent = play_duration / runtime_seconds * 100
        if percent >= minimum_percent:
            return True, f"{percent:.0f}% >= {minimum_percent}%"
    return False, (
        f"durée {play_duration}s sous les seuils "
        f"(minimum_duration={minimum_duration}s, minimum_percent={minimum_percent}%)"
    )


def _mbps(bits) -> float | None:
    return round(int(bits) / 1_000_000, 1) if bits else None


def _is_lan(remote: str | None) -> bool | None:
    """Devine si l'IP distante est sur le réseau local (RFC1918 / loopback)."""
    if not remote:
        return None
    host = remote.rsplit(":", 1)[0] if remote.count(":") == 1 else remote
    host = host.strip("[]")
    if host.startswith(("10.", "192.168.", "127.", "169.254.", "::1", "fd", "fe80")):
        return True
    if host.startswith("172."):
        try:
            return 16 <= int(host.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def _stream_details(session: dict, item: dict) -> dict:
    """Détails techniques de la lecture (modèle « now playing » de Tautulli) :
    produit, lecteur, qualité, container/vidéo/audio source→cible, raisons de
    transcodage, localisation. Recalculé à chaque poll (l'état de transcodage
    peut changer en cours de lecture)."""
    ps = session.get("PlayState", {}) or {}
    ti = session.get("TranscodingInfo") or {}
    streams = item.get("MediaStreams", []) or []

    def pick(stype, idx):
        if idx is not None:
            m = next((s for s in streams
                      if s.get("Type") == stype and s.get("Index") == idx), None)
            if m:
                return m
        return next((s for s in streams if s.get("Type") == stype), {})

    video = pick("Video", None)
    audio = pick("Audio", ps.get("AudioStreamIndex"))
    sub_idx = ps.get("SubtitleStreamIndex")
    sub = pick("Subtitle", sub_idx) if sub_idx not in (None, -1) else {}

    method = ps.get("PlayMethod") or ("Transcode" if ti else None)
    transcoding = method == "Transcode"
    reasons = ti.get("TranscodeReasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]

    def up(s):
        return (s or "").upper() or None

    src_res = resolution_label(video.get("Width"), video.get("Height"))
    tgt_res = resolution_label(ti.get("Width"), ti.get("Height")) if transcoding else None
    a_chan = audio.get("ChannelLayout") or (
        f"{audio.get('Channels')}ch" if audio.get("Channels") else None)
    bitrate = ti.get("Bitrate") or video.get("BitRate") or item.get("Bitrate")

    return {
        "product": session.get("Client"),
        "player": session.get("DeviceName"),
        "transcoding": transcoding,
        "stream_method": method,
        "transcode_reasons": reasons,
        "transcode_progress": ti.get("CompletionPercentage"),
        "quality_mbps": _mbps(bitrate),
        "bandwidth_mbps": _mbps(ti.get("Bitrate")) if transcoding else _mbps(bitrate),
        "container_src": up(item.get("Container", "").split(",")[0]),
        "container_tgt": up(ti.get("Container")) if transcoding else None,
        "video_direct": bool(ti.get("IsVideoDirect")) if transcoding else True,
        "video_src": " ".join(x for x in (up(video.get("Codec")), src_res) if x) or None,
        "video_tgt": (" ".join(x for x in (up(ti.get("VideoCodec")), tgt_res) if x) or None)
        if transcoding else None,
        "audio_direct": bool(ti.get("IsAudioDirect")) if transcoding else True,
        "audio_src": " ".join(
            x for x in (up(audio.get("Codec")), a_chan, audio.get("Language")) if x) or None,
        "audio_tgt": up(ti.get("AudioCodec")) if transcoding else None,
        "subtitle": sub.get("DisplayTitle") or sub.get("Language") if sub else None,
        "ip_address": session.get("RemoteEndPoint"),
        "is_lan": _is_lan(session.get("RemoteEndPoint")),
    }


@dataclass
class TrackedSession:
    key: str
    session_id: str
    jellyfin_user_id: str
    user_name: str
    item_id: str
    item_type: str
    item_name: str
    series_id: str | None
    series_name: str | None
    season_number: int | None
    episode_number: int | None
    runtime_seconds: int | None
    client_name: str | None
    device_name: str | None
    ip_address: str | None
    play_method: str | None
    video_resolution: str | None
    video_codec: str | None
    audio_codec: str | None
    genres: list[str] = field(default_factory=list)
    started_at: str = ""
    watched_seconds: float = 0.0
    position_ticks: int = 0
    paused: bool = False
    last_seen: float = 0.0
    # Instant (monotonic) où la session a disparu du polling ; 0 = présente.
    # Sert au délai de grâce avant finalisation (cf. poll).
    missing_since: float = 0.0
    info: dict = field(default_factory=dict)  # détails « now playing » (transcodage…)

    def percent_complete(self) -> float | None:
        if not self.runtime_seconds:
            return None
        return min(100.0, self.position_ticks / TICKS_PER_SECOND
                   / self.runtime_seconds * 100)


class ActivityMonitor:
    def __init__(self, config):
        self.config = config
        self._sessions: dict[str, TrackedSession] = {}
        self._lock = threading.Lock()

    # --- Polling ------------------------------------------------------------

    def poll(self, api) -> None:
        now = time.monotonic()
        current: dict[str, tuple[dict, dict]] = {}
        for session in api.get_sessions():
            item = session.get("NowPlayingItem")
            if not item or not session.get("UserId"):
                continue
            key = f"{session['Id']}|{item['Id']}"
            current[key] = (session, item)

        with self._lock:
            for key, (session, item) in current.items():
                tracked = self._sessions.get(key)
                play_state = session.get("PlayState", {})
                if tracked is None:
                    tracked = self._new_tracked(key, session, item, now)
                    self._sessions[key] = tracked
                    logger.info("Lecture démarrée : %s par %s",
                                tracked.item_name, tracked.user_name)
                else:
                    delta = now - tracked.last_seen
                    if not tracked.paused:
                        # Borne le delta : si le poller a sauté des cycles, on
                        # ne crédite pas plus de 3 intervalles d'un coup.
                        tracked.watched_seconds += min(
                            delta, self.config.poll_interval * 3)
                tracked.paused = bool(play_state.get("IsPaused"))
                tracked.position_ticks = play_state.get("PositionTicks") or 0
                tracked.play_method = play_state.get("PlayMethod") or tracked.play_method
                tracked.info = _stream_details(session, item)
                tracked.last_seen = now
                tracked.missing_since = 0.0  # de retour : annule un éventuel délai

            # Délai de grâce : une session Jellyfin disparaît parfois du polling
            # un cycle ou deux (client Android, NowPlayingItem null transitoire,
            # blip réseau) alors que la lecture continue. La finaliser aussitôt
            # fragmenterait une lecture continue en multiples entrées. On attend
            # donc qu'elle soit absente depuis ``session_grace`` secondes.
            grace = self.config.session_grace
            for key in [k for k in self._sessions if k not in current]:
                tracked = self._sessions[key]
                if not tracked.missing_since:
                    tracked.missing_since = now
                if now - tracked.missing_since >= grace:
                    self._finalize(self._sessions.pop(key))

    def _new_tracked(self, key, session, item, now) -> TrackedSession:
        play_state = session.get("PlayState", {})
        runtime_ticks = item.get("RunTimeTicks")
        video_stream = next(
            (s for s in item.get("MediaStreams", []) if s.get("Type") == "Video"), {})
        audio_stream = next(
            (s for s in item.get("MediaStreams", []) if s.get("Type") == "Audio"), {})
        # Enrichissement depuis le cache items (bibliothèque, genres) si connu.
        cached = database.query_one(
            "SELECT library_id, genres, runtime_seconds FROM items WHERE item_id = ?",
            (item["Id"],),
        ) or {}
        genres = item.get("Genres") or (
            json.loads(cached["genres"]) if cached.get("genres") else [])
        return TrackedSession(
            key=key,
            session_id=session["Id"],
            jellyfin_user_id=session["UserId"],
            user_name=session.get("UserName", "?"),
            item_id=item["Id"],
            item_type=item.get("Type", "Unknown"),
            item_name=item.get("Name", "?"),
            series_id=item.get("SeriesId"),
            series_name=item.get("SeriesName"),
            season_number=item.get("ParentIndexNumber"),
            episode_number=item.get("IndexNumber"),
            runtime_seconds=(runtime_ticks // TICKS_PER_SECOND if runtime_ticks
                             else cached.get("runtime_seconds")),
            client_name=session.get("Client"),
            device_name=session.get("DeviceName"),
            ip_address=session.get("RemoteEndPoint"),
            play_method=play_state.get("PlayMethod"),
            video_resolution=resolution_label(
                video_stream.get("Width") or item.get("Width"),
                video_stream.get("Height") or item.get("Height")),
            video_codec=video_stream.get("Codec"),
            audio_codec=audio_stream.get("Codec"),
            genres=genres,
            started_at=now_iso(),
            position_ticks=play_state.get("PositionTicks") or 0,
            paused=bool(play_state.get("IsPaused")),
            last_seen=now,
            info=_stream_details(session, item),
        )

    # --- Finalisation ---------------------------------------------------------

    def _finalize(self, tracked: TrackedSession) -> None:
        play_duration = int(tracked.watched_seconds)
        record, reason = should_record(
            play_duration, tracked.runtime_seconds,
            self.config.minimum_duration, self.config.minimum_percent)
        if not record:
            logger.debug("Session ignorée (%s par %s) : %s",
                         tracked.item_name, tracked.user_name, reason)
            return

        library = database.query_one(
            """
            SELECT l.library_id, l.name FROM items i
            JOIN libraries l ON l.library_id = i.library_id
            WHERE i.item_id = ?
            """,
            (tracked.item_id,),
        )
        with database.db() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO session_history
                    (session_key, source, jellyfin_user_id, user_name, item_id,
                     item_type, item_name, series_name, season_number, episode_number,
                     library_id, library_name, started_at, stopped_at, play_duration,
                     runtime_seconds, percent_complete, client_name, device_name,
                     ip_address, play_method, video_resolution, video_codec,
                     audio_codec, genres)
                VALUES (?, 'live', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tracked.key, tracked.jellyfin_user_id, tracked.user_name,
                    tracked.item_id, tracked.item_type, tracked.item_name,
                    tracked.series_name, tracked.season_number, tracked.episode_number,
                    library["library_id"] if library else None,
                    library["name"] if library else None,
                    tracked.started_at, now_iso(), play_duration,
                    tracked.runtime_seconds, tracked.percent_complete(),
                    tracked.client_name, tracked.device_name, tracked.ip_address,
                    tracked.play_method, tracked.video_resolution,
                    tracked.video_codec, tracked.audio_codec,
                    json.dumps(tracked.genres) if tracked.genres else None,
                ),
            )
            conn.execute(
                "UPDATE users SET last_activity = ? WHERE jellyfin_user_id = ?",
                (now_iso(), tracked.jellyfin_user_id),
            )
        logger.info("Session enregistrée : %s par %s (%ds vus, %s)",
                    tracked.item_name, tracked.user_name, play_duration, reason)

    # --- Webhook ------------------------------------------------------------

    def handle_webhook(self, payload: dict) -> None:
        """Événements du plugin Jellyfin Webhook (PlaybackStart/Stop/Progress).

        Le polling reste la source de vérité ; ici on réagit immédiatement aux
        pauses/reprises et aux stops sans attendre le prochain cycle.
        """
        notif = payload.get("NotificationType", "")
        user_id = payload.get("UserId")
        item_id = payload.get("ItemId")
        if not user_id or not item_id:
            return
        with self._lock:
            matches = [t for t in self._sessions.values()
                       if t.jellyfin_user_id == user_id and t.item_id == item_id]
            if notif == "PlaybackStop":
                for t in matches:
                    self._sessions.pop(t.key, None)
            elif notif == "PlaybackProgress":
                paused = payload.get("IsPaused")
                if paused is not None:
                    for t in matches:
                        t.paused = bool(paused)
        if notif == "PlaybackStop":
            for t in matches:
                self._finalize(t)

    # --- Lecture pour le dashboard ---------------------------------------------

    def snapshot(self, user_id: str | None = None) -> list[dict]:
        """Sessions actives ; filtrées sur un utilisateur si demandé."""
        with self._lock:
            sessions = list(self._sessions.values())
        out = []
        for t in sessions:
            if user_id and t.jellyfin_user_id != user_id:
                continue
            out.append({
                "user_name": t.user_name,
                "jellyfin_user_id": t.jellyfin_user_id,
                "item_id": t.item_id,
                "series_id": t.series_id,
                "item_name": t.item_name,
                "item_type": t.item_type,
                "series_name": t.series_name,
                "season_number": t.season_number,
                "episode_number": t.episode_number,
                "client_name": t.client_name,
                "device_name": t.device_name,
                "play_method": t.play_method,
                "video_resolution": t.video_resolution,
                "paused": t.paused,
                "watched_seconds": int(t.watched_seconds),
                "runtime_seconds": t.runtime_seconds,
                "percent_complete": t.percent_complete(),
                "started_at": t.started_at,
                **t.info,
            })
        return sorted(out, key=lambda s: s["started_at"], reverse=True)
