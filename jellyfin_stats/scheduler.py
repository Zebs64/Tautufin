"""Tâches planifiées (boucles asyncio, démarrées dans le lifespan FastAPI) :

- polling de /Sessions toutes les ``poll_interval`` secondes ;
- synchronisation utilisateurs / bibliothèques / médias toutes les
  ``sync_interval`` secondes ;
- purge horaire des sessions HTTP expirées.
"""

import asyncio
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from . import auth, database
from .activity import TICKS_PER_SECOND, resolution_label
from .database import now_iso
from .jellyfin_api import JellyfinError

logger = logging.getLogger(__name__)


# Rôles conservés pour les tops (acteurs / réalisateurs / scénaristes), avec un
# plafond par média pour ne pas gonfler la base avec la figuration.
_PEOPLE_TYPES = {"Actor", "Director", "Writer", "GuestStar"}
_PEOPLE_MAX = 25
_SYNC_OVERLAP = timedelta(minutes=5)
_SYNC_ERROR_MAX = 240


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0) \
        .isoformat().replace("+00:00", "Z")


def _incremental_start(cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        parsed = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Curseur de synchronisation invalide, full sync requise")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        logger.warning("Curseur de synchronisation non UTC, full sync requise")
        return None
    return _format_utc(parsed - _SYNC_OVERLAP)


def _sanitize_sync_error(exc: Exception) -> str:
    """Message utilisateur borné, sans URL ni secret courant."""
    message = re.sub(r"https?://\S+", "[adresse masquée]", str(exc), flags=re.I)
    message = re.sub(
        r"(?i)\b(api[_-]?key|token|password|credential)\s*[=:]\s*\S+",
        r"\1=[masqué]",
        message,
    )
    message = " ".join(message.split()) or "Erreur de synchronisation"
    return message[:_SYNC_ERROR_MAX]


def _people_json(people) -> str | None:
    """Sérialise la distribution/équipe utile d'un média (Name + Type)."""
    if not people:
        return None
    kept = [{"Name": p["Name"], "Type": p.get("Type")}
            for p in people
            if p.get("Name") and p.get("Type") in _PEOPLE_TYPES]
    return json.dumps(kept[:_PEOPLE_MAX], ensure_ascii=False) if kept else None


def sync_users(api) -> int:
    users = api.get_users()
    with database.db() as conn:
        for user in users:
            conn.execute(
                """
                INSERT INTO users (jellyfin_user_id, username, is_admin)
                VALUES (?, ?, ?)
                ON CONFLICT(jellyfin_user_id) DO UPDATE SET
                    username = excluded.username,
                    is_admin = excluded.is_admin,
                    is_active = 1
                """,
                (
                    user["Id"],
                    user.get("Name", "?"),
                    int(bool(user.get("Policy", {}).get("IsAdministrator"))),
                ),
            )
        # Les utilisateurs disparus de Jellyfin sont marqués inactifs ;
        # leur historique est conservé (exigence spec).
        ids = [u["Id"] for u in users]
        placeholders = ",".join("?" * len(ids)) or "''"
        conn.execute(
            f"UPDATE users SET is_active = 0 WHERE jellyfin_user_id NOT IN ({placeholders})",
            ids,
        )
    return len(users)


def sync_libraries_and_items(api, report=None,
                             min_date_last_saved: str | None = None,
                             metrics: dict | None = None) -> int:
    total_items = 0
    total_changed = 0
    folders = [f for f in api.get_libraries() if f.get("ItemId")]
    if metrics is not None:
        metrics.update(libraries=len(folders), items_received=0, items_changed=0)
    for idx, folder in enumerate(folders):
        if report:
            report(phase="libraries", current=idx, total=len(folders),
                   label=folder.get("Name", "?"), libraries=len(folders))
        library_id = folder["ItemId"]
        # Phase réseau HORS transaction : récupérer les médias sans tenir le
        # verrou d'écriture SQLite pendant les appels HTTP (sinon le poller et
        # les sessions HTTP se prennent un « database is locked »).
        items = list(api.iter_items(
            library_id, min_date_last_saved=min_date_last_saved))
        count = len(items)
        # Phase écriture : transaction courte.
        with database.db() as conn:
            for item in items:
                runtime_ticks = item.get("RunTimeTicks")
                video = next((s for s in item.get("MediaStreams", [])
                              if s.get("Type") == "Video"), {})
                audio = next((s for s in item.get("MediaStreams", [])
                              if s.get("Type") == "Audio"), {})
                changed = conn.execute(
                    """
                    INSERT INTO items
                        (item_id, library_id, name, type, series_name,
                         season_number, episode_number, year, genres,
                         runtime_seconds, video_resolution, video_codec,
                         audio_codec, people, added_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(item_id) DO UPDATE SET
                        library_id = excluded.library_id,
                        name = excluded.name,
                        type = excluded.type,
                        series_name = excluded.series_name,
                        season_number = excluded.season_number,
                        episode_number = excluded.episode_number,
                        year = excluded.year,
                        genres = excluded.genres,
                        runtime_seconds = excluded.runtime_seconds,
                        video_resolution = excluded.video_resolution,
                        video_codec = excluded.video_codec,
                        audio_codec = excluded.audio_codec,
                        people = excluded.people,
                        added_at = excluded.added_at,
                        updated_at = excluded.updated_at
                    WHERE items.library_id IS NOT excluded.library_id
                       OR items.name IS NOT excluded.name
                       OR items.type IS NOT excluded.type
                       OR items.series_name IS NOT excluded.series_name
                       OR items.season_number IS NOT excluded.season_number
                       OR items.episode_number IS NOT excluded.episode_number
                       OR items.year IS NOT excluded.year
                       OR items.genres IS NOT excluded.genres
                       OR items.runtime_seconds IS NOT excluded.runtime_seconds
                       OR items.video_resolution IS NOT excluded.video_resolution
                       OR items.video_codec IS NOT excluded.video_codec
                       OR items.audio_codec IS NOT excluded.audio_codec
                       OR items.people IS NOT excluded.people
                       OR items.added_at IS NOT excluded.added_at
                    """,
                    (
                        item["Id"], library_id, item.get("Name"), item.get("Type"),
                        item.get("SeriesName"), item.get("ParentIndexNumber"),
                        item.get("IndexNumber"), item.get("ProductionYear"),
                        json.dumps(item["Genres"]) if item.get("Genres") else None,
                        runtime_ticks // TICKS_PER_SECOND if runtime_ticks else None,
                        resolution_label(video.get("Width"), video.get("Height")),
                        video.get("Codec"), audio.get("Codec"),
                        _people_json(item.get("People")),
                        (item.get("DateCreated") or "").replace("T", " ")[:19] or None,
                        now_iso(),
                    ),
                )
                total_changed += changed.rowcount
            item_count_update = "" if min_date_last_saved else \
                "item_count = excluded.item_count,"
            conn.execute(
                f"""
                INSERT INTO libraries (library_id, name, collection_type, item_count, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(library_id) DO UPDATE SET
                    name = excluded.name,
                    collection_type = excluded.collection_type,
                    {item_count_update}
                    updated_at = excluded.updated_at
                """,
                (library_id, folder.get("Name", "?"),
                 folder.get("CollectionType"), count, now_iso()),
            )
        total_items += count
        if metrics is not None:
            metrics.update(items_received=total_items, items_changed=total_changed)
        if report:
            report(items=total_items, items_changed=total_changed)
    if report:
        report(current=len(folders), label="")
    return total_items


def sync_all(api, report=None, force_full: bool = False) -> dict:
    started_at = _utc_now()
    cursor = database.get_sync_cursor()
    incremental_start = None if force_full else _incremental_start(cursor)
    mode = "incremental" if incremental_start else "full"
    started_text = _format_utc(started_at)
    health = database.get_sync_health()
    health.update(
        status="running", mode=mode, phase="users", started_at=started_text,
        last_attempt_at=started_text, finished_at=None, duration_seconds=None,
        users=0, libraries=0, items_received=0, items_changed=0,
        error=None, cursor_preserved=False,
    )
    database.set_sync_health(health)

    def report_progress(**values) -> None:
        if report:
            report(**values)
        progress = database.get_sync_health()
        progress["status"] = "running"
        if "phase" in values:
            progress["phase"] = values["phase"]
        if "users" in values:
            progress["users"] = values["users"]
        if "libraries" in values:
            progress["libraries"] = values["libraries"]
        if "items" in values:
            progress["items_received"] = values["items"]
        if "items_changed" in values:
            progress["items_changed"] = values["items_changed"]
        database.set_sync_health(progress)

    metrics: dict = {}
    try:
        report_progress(phase="users", label="Utilisateurs")
        users = sync_users(api)
        report_progress(users=users)
        items = sync_libraries_and_items(
            api, report=report_progress, min_date_last_saved=incremental_start,
            metrics=metrics,
        )
        finished_at = _utc_now()
        database.set_sync_cursor(started_text)
        health = database.get_sync_health()
        health.update(
            status="success", phase="done", finished_at=_format_utc(finished_at),
            last_success_at=_format_utc(finished_at),
            duration_seconds=round((finished_at - started_at).total_seconds(), 3),
            users=users, libraries=metrics.get("libraries", 0),
            items_received=items, items_changed=metrics.get("items_changed", 0),
            error=None, cursor_preserved=False,
        )
        database.set_sync_health(health)
        logger.info(
            "Synchronisation Jellyfin : %d utilisateurs, %d bibliothèques, "
            "%d médias reçus, %d insérés/modifiés",
            users, health["libraries"], items, health["items_changed"],
        )
        return {
            "users": users,
            "libraries": health["libraries"],
            "items": items,
            "items_changed": health["items_changed"],
        }
    except Exception as exc:
        finished_at = _utc_now()
        health = database.get_sync_health()
        health.update(
            status="error", phase="error", finished_at=_format_utc(finished_at),
            duration_seconds=round((finished_at - started_at).total_seconds(), 3),
            error=_sanitize_sync_error(exc), cursor_preserved=True,
        )
        database.set_sync_health(health)
        logger.exception("Synchronisation Jellyfin échouée")
        raise


# --- Suivi de progression d'une synchro en arrière-plan --------------------
# État partagé (manuel + périodique) lu par l'API pour la barre de progression.
# La synchro tourne dans un thread daemon : elle survit à la navigation côté
# client (la requête HTTP qui la déclenche ne fait que la lancer).
_sync_lock = threading.Lock()
_sync_state = {
    "running": False, "phase": "idle", "label": "",
    "current": 0, "total": 0, "users": 0, "items": 0,
    "error": None, "finished_at": None,
}


def get_sync_state() -> dict:
    with _sync_lock:
        return dict(_sync_state)


def get_sync_status() -> dict:
    """Progression historique inchangée, enrichie de la santé persistante."""
    return {**get_sync_state(), "health": database.get_sync_health()}


def _sync_set(**kw) -> None:
    with _sync_lock:
        _sync_state.update(kw)


def start_sync(api, force_full: bool = True) -> bool:
    """Lance une synchro en arrière-plan si aucune n'est déjà en cours.
    Retourne True si elle a été démarrée, False si une synchro tournait déjà."""
    with _sync_lock:
        if _sync_state["running"]:
            return False
        _sync_state.update(running=True, phase="users", label="Utilisateurs",
                           current=0, total=0, users=0, items=0,
                           error=None, finished_at=None)

    def _run():
        try:
            result = sync_all(api, report=_sync_set, force_full=force_full)
            _sync_set(phase="done", label="", users=result["users"],
                      items=result["items"])
        except Exception as exc:  # noqa: BLE001 — on remonte l'erreur à l'UI
            logger.exception("Synchronisation en arrière-plan échouée")
            _sync_set(phase="error", error=_sanitize_sync_error(exc))
        finally:
            _sync_set(running=False, finished_at=now_iso())

    threading.Thread(target=_run, name="sync", daemon=True).start()
    return True


class Scheduler:
    def __init__(self, config, api, monitor):
        self.config = config
        self.api = api
        self.monitor = monitor
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._poll_loop()),
            asyncio.create_task(self._sync_loop()),
            asyncio.create_task(self._cleanup_loop()),
        ]
        logger.info("Scheduler démarré (poll: %ds, sync: %ds)",
                    self.config.poll_interval, self.config.sync_interval)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        # Finalise proprement les sessions encore suivies à l'arrêt.
        if self.config.jellyfin_configured:
            with_sessions = list(self.monitor._sessions.values())
            for tracked in with_sessions:
                self.monitor._sessions.pop(tracked.key, None)
                await asyncio.to_thread(self.monitor._finalize, tracked)

    async def _poll_loop(self) -> None:
        while True:
            if self.config.jellyfin_configured:
                try:
                    await asyncio.to_thread(self.monitor.poll, self.api)
                except JellyfinError as exc:
                    logger.warning("Polling /Sessions impossible : %s", exc)
                except Exception:
                    logger.exception("Erreur inattendue du poller")
            await asyncio.sleep(self.config.poll_interval)

    async def _sync_loop(self) -> None:
        while True:
            if self.config.jellyfin_configured:
                # Synchro périodique via le même chemin que la synchro manuelle
                # (thread daemon + état de progression partagé) ; ignorée si une
                # synchro tourne déjà.
                start_sync(self.api, force_full=False)
            await asyncio.sleep(self.config.sync_interval)

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                purged = await asyncio.to_thread(auth.purge_expired_sessions)
                if purged:
                    logger.debug("%d sessions HTTP expirées purgées", purged)
            except Exception:
                logger.exception("Erreur de purge des sessions")
            await asyncio.sleep(3600)
