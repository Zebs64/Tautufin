"""Import depuis un backup Streamystats.

Streamystats (https://github.com/fredrikburmester/streamystats) exporte un
fichier JSON « Backup » de structure ::

    {
      "exportInfo": {"version": "streamystats", "exportType": "backup", ...},
      "sessions": [ {startTime, userId, itemId, playDuration, ...}, ... ],
      "server": {...}, "hiddenRecommendations": [...]
    }

Seul le tableau ``sessions`` nous intéresse : ce sont des lectures réelles
enregistrées par Streamystats. Mapping vers ``session_history`` (source
``'import'``, comme Playback Reporting) :

    startTime           → started_at          endTime          → stopped_at
    userId              → jellyfin_user_id    itemId           → item_id
    playDuration (sec)  → play_duration       runtimeTicks/1e7 → runtime_seconds
    percentComplete     → percent_complete    clientName       → client_name
    deviceName          → device_name         remoteEndPoint   → ip_address
    playMethod          → play_method (Transcode si isTranscoded)
    resolutionWidth/Height → video_resolution videoCodec/audioCodec → codecs

Le type de média, la saison/épisode, les genres et la bibliothèque ne figurent
pas dans le backup : ils sont enrichis depuis le cache ``items`` (via itemId).

Idempotence : index UNIQUE (jellyfin_user_id, item_id, started_at) + INSERT OR
IGNORE — déduplication y compris vis-à-vis d'un import Playback Reporting des
mêmes lectures. Les filtres de durée minimale ([Monitoring]) s'appliquent comme
pour les autres imports.
"""

import json
import logging
import os
from collections import Counter

from . import database
from .activity import TICKS_PER_SECOND, resolution_label, should_record
from .import_playback import ImportError_, _normalize_date
from .jellyfin_api import JellyfinError

logger = logging.getLogger(__name__)


def looks_like_streamystats(path: str) -> bool:
    """Détection rapide (route d'import) : un backup Streamystats est un JSON,
    donc commence par « { », là où un .db commence par la signature SQLite et
    un backup .tsv par une date. La validation fine est faite par analyze()."""
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            return f.read(4096).lstrip()[:1] == b"{"
    except OSError:
        return False


def _load_backup(path: str) -> dict:
    if not path or not os.path.isfile(path):
        raise ImportError_(f"Fichier introuvable : {path!r}")
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ImportError_(f"JSON illisible : {exc}") from exc
    if not isinstance(data, dict):
        raise ImportError_("Backup Streamystats attendu (objet JSON)")
    info = data.get("exportInfo") or {}
    if info.get("version") != "streamystats":
        raise ImportError_(
            "Ce JSON n'est pas un backup Streamystats "
            f"(exportInfo.version = {info.get('version')!r}, attendu « streamystats »)")
    if not isinstance(data.get("sessions"), list):
        raise ImportError_("Backup Streamystats invalide : tableau « sessions » absent")
    return data


def analyze(path: str) -> dict:
    """Aperçu avant import : volume, plage de dates, utilisateurs, serveur."""
    data = _load_backup(path)
    sessions = data["sessions"]
    dates = sorted(_normalize_date(s["startTime"]) for s in sessions
                   if s.get("startTime"))
    per_user = Counter(s.get("userId") or "" for s in sessions)
    known = {
        u["jellyfin_user_id"]: u["username"]
        for u in database.query("SELECT jellyfin_user_id, username FROM users")
    }
    return {
        "format": "streamystats",
        "entries": len(sessions),
        "malformed": 0,
        "server_name": (data.get("exportInfo") or {}).get("serverName"),
        "date_min": dates[0] if dates else None,
        "date_max": dates[-1] if dates else None,
        "users": [
            {"user_id": uid, "entries": n, "username": known.get(uid)}
            for uid, n in per_user.most_common()
        ],
    }


def _items_cache() -> dict[str, dict]:
    rows = database.query(
        """
        SELECT i.item_id, i.library_id, l.name AS library_name, i.type,
               i.series_name, i.season_number, i.episode_number,
               i.runtime_seconds, i.genres, i.video_resolution,
               i.video_codec, i.audio_codec
        FROM items i LEFT JOIN libraries l ON l.library_id = i.library_id
        """)
    return {r["item_id"]: r for r in rows}


def run_import(path: str, api, minimum_duration: int, minimum_percent: int) -> dict:
    """Migre les sessions d'un backup Streamystats vers session_history.

    Rapport : importées / doublons ignorés / filtrées (durée minimale) / erreurs.
    """
    data = _load_backup(path)
    sessions = data["sessions"]
    items = _items_cache()

    # Résolution des noms d'utilisateurs : cache local, complété par l'API si
    # configurée (un userId absent du cache reste affichable).
    usernames: dict[str, str] = {
        u["jellyfin_user_id"]: u["username"]
        for u in database.query("SELECT jellyfin_user_id, username FROM users")
    }
    try:
        for jf_user in api.get_users():
            usernames[jf_user["Id"]] = jf_user.get("Name", jf_user["Id"])
    except JellyfinError as exc:
        logger.warning("Résolution des utilisateurs via Jellyfin impossible : %s", exc)

    report = {"imported": 0, "duplicates": 0, "filtered": 0,
              "errors": 0, "format": "streamystats"}

    with database.db() as conn:
        for s in sessions:
            try:
                user_id = s.get("userId")
                started_raw = s.get("startTime")
                # Sans utilisateur ni date de début, la session est inexploitable
                # (jellyfin_user_id est NOT NULL, started_at sert d'ancre dedup).
                if not user_id or not started_raw:
                    report["errors"] += 1
                    continue

                started_at = _normalize_date(started_raw)
                stopped_at = _normalize_date(s["endTime"]) if s.get("endTime") else None
                item_id = s.get("itemId")
                cached = items.get(item_id, {}) if item_id else {}

                runtime_ticks = s.get("runtimeTicks")
                runtime_seconds = (runtime_ticks // TICKS_PER_SECOND
                                   if runtime_ticks
                                   else cached.get("runtime_seconds"))
                play_duration = int(s.get("playDuration") or 0)

                record, reason = should_record(
                    play_duration, runtime_seconds, minimum_duration, minimum_percent)
                if not record:
                    report["filtered"] += 1
                    continue

                percent = s.get("percentComplete")
                if percent is None and runtime_seconds:
                    percent = min(100.0, play_duration / runtime_seconds * 100)

                resolution = resolution_label(
                    s.get("resolutionWidth"), s.get("resolutionHeight")) \
                    or cached.get("video_resolution")
                play_method = s.get("playMethod") or (
                    "Transcode" if s.get("isTranscoded") else None)

                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO session_history
                        (session_key, source, jellyfin_user_id, user_name, item_id,
                         item_type, item_name, series_name, season_number,
                         episode_number, library_id, library_name, started_at,
                         stopped_at, play_duration, runtime_seconds, percent_complete,
                         client_name, device_name, ip_address, play_method,
                         video_resolution, video_codec, audio_codec, genres)
                    VALUES (?, 'import', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        s.get("id"), user_id,
                        usernames.get(user_id, (user_id or "")[:8] or "?"),
                        item_id, cached.get("type"),
                        s.get("itemName"),
                        s.get("seriesName") or cached.get("series_name"),
                        cached.get("season_number"), cached.get("episode_number"),
                        cached.get("library_id"), cached.get("library_name"),
                        started_at, stopped_at, play_duration, runtime_seconds,
                        percent, s.get("clientName"), s.get("deviceName"),
                        s.get("remoteEndPoint"), play_method, resolution,
                        s.get("videoCodec") or cached.get("video_codec"),
                        s.get("audioCodec") or cached.get("audio_codec"),
                        cached.get("genres"),
                    ),
                )
                if cursor.rowcount:
                    report["imported"] += 1
                else:
                    report["duplicates"] += 1
            except Exception:
                logger.exception("Import Streamystats : session en erreur (%s)",
                                 s.get("id"))
                report["errors"] += 1

    logger.info(
        "Import Streamystats terminé : %(imported)d importées, %(duplicates)d "
        "doublons, %(filtered)d filtrées, %(errors)d erreurs", report)
    return report
