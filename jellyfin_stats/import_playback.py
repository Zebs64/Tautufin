"""Import depuis le plugin Jellyfin "Playback Reporting".

Deux formats de source, détectés automatiquement au contenu :
- la base SQLite du plugin (``playback_reporting.db``), table PlaybackActivity ;
- un fichier de backup du plugin (``PlaybackReportingBackup-*.tsv``) : pas
  d'en-tête, 9 colonnes séparées par tabulations dans l'ordre de la table
  (cf. ActivityRepository.ExportRawData/ImportRawData du plugin), les lignes
  mal formées sont ignorées comme le fait le plugin lui-même.

Colonnes : DateCreated, UserId, ItemId, ItemType, ItemName, PlaybackMethod,
ClientName, DeviceName, PlayDuration (temps réellement regardé, en secondes).

Idempotence : l'index UNIQUE (jellyfin_user_id, item_id, started_at) +
INSERT OR IGNORE — la clé de déduplication est DateCreated + UserId + ItemId,
y compris entre un import .db et un import .tsv des mêmes données.
Les filtres de durée minimale (config [Monitoring]) s'appliquent comme pour
les sessions capturées en direct.
"""

import json
import logging
import os
import sqlite3
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import database
from .activity import should_record
from .jellyfin_api import JellyfinError

logger = logging.getLogger(__name__)

SQLITE_MAGIC = b"SQLite format 3\x00"

TSV_COLUMNS = ["DateCreated", "UserId", "ItemId", "ItemType", "ItemName",
               "PlaybackMethod", "ClientName", "DeviceName", "PlayDuration"]


def _resolve_display_tz() -> ZoneInfo | None:
    """Fuseau de référence des horodatages stockés. On suit la variable TZ si
    elle est posée, sinon Europe/Paris (fuseau du serveur source). Sert à
    recaler les timestamps horodatés en UTC (backup Streamystats) sur le même
    fuseau que l'heure locale écrite par Playback Reporting et le monitoring."""
    for name in (os.environ.get("TZ"), "Europe/Paris"):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return None


_DISPLAY_TZ = _resolve_display_tz()


class ImportError_(Exception):
    pass


def _normalize_date(value: str) -> str:
    """Horodatage → 'YYYY-MM-DD HH:MM:SS' (naïf, heure locale).

    Un timestamp portant un fuseau (ex. Streamystats en UTC : « …T…Z » ou
    « +00:00 ») est *converti* vers le fuseau d'affichage avant d'être rendu
    naïf — sans ça, le décalage UTC↔local créait des doublons (mêmes lectures
    déjà importées en heure locale par Playback Reporting, à 1–2 h d'écart).
    Un timestamp déjà naïf (Playback Reporting) est laissé tel quel."""
    raw = (value or "").strip()
    if not raw:
        return ""
    iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = None
    if dt is not None and dt.tzinfo is not None:
        if _DISPLAY_TZ is not None:
            dt = dt.astimezone(_DISPLAY_TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    # Déjà naïf (ou format inattendu) : ancien comportement de troncature.
    return raw.replace("T", " ").split(".")[0][:19]


# --- Chargement des sources ---------------------------------------------------

def _load_sqlite(path: str) -> list[dict]:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT DateCreated, UserId, ItemId, ItemType, ItemName,"
            " PlaybackMethod, ClientName, DeviceName, PlayDuration"
            " FROM PlaybackActivity ORDER BY DateCreated"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        raise ImportError_(
            f"Base Playback Reporting invalide ({exc}) — table PlaybackActivity attendue"
        ) from exc


def _load_tsv(path: str) -> tuple[list[dict], int]:
    """Backup .tsv du plugin → (lignes valides, lignes mal formées)."""
    rows, malformed = [], 0
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            tokens = line.split("\t")
            if len(tokens) != len(TSV_COLUMNS):
                malformed += 1
                continue
            rows.append(dict(zip(TSV_COLUMNS, tokens)))
    if not rows and malformed == 0:
        raise ImportError_("Fichier vide — backup Playback Reporting attendu")
    if not rows:
        raise ImportError_(
            "Aucune ligne exploitable — un backup .tsv du plugin contient"
            " 9 colonnes séparées par des tabulations, sans en-tête")
    rows.sort(key=lambda r: r["DateCreated"])
    return rows, malformed


def load_source(path: str) -> tuple[list[dict], int, str]:
    """Détecte le format au contenu ; retourne (lignes, mal formées, format)."""
    if not path or not os.path.isfile(path):
        raise ImportError_(f"Fichier introuvable : {path!r}")
    with open(path, "rb") as f:
        magic = f.read(len(SQLITE_MAGIC))
    if magic == SQLITE_MAGIC:
        return _load_sqlite(path), 0, "sqlite"
    rows, malformed = _load_tsv(path)
    return rows, malformed, "tsv"


def analyze(path: str) -> dict:
    """Aperçu avant import : volume, plage de dates, utilisateurs présents."""
    rows, malformed, source_format = load_source(path)
    per_user = Counter(r["UserId"] or "" for r in rows)
    known = {
        u["jellyfin_user_id"]: u["username"]
        for u in database.query("SELECT jellyfin_user_id, username FROM users")
    }
    return {
        "format": source_format,
        "entries": len(rows),
        "malformed": malformed,
        "date_min": _normalize_date(rows[0]["DateCreated"]) if rows else None,
        "date_max": _normalize_date(rows[-1]["DateCreated"]) if rows else None,
        "users": [
            {"user_id": uid, "entries": n, "username": known.get(uid)}
            for uid, n in per_user.most_common()
        ],
    }


def run_import(path: str, api, minimum_duration: int, minimum_percent: int) -> dict:
    """Migre les données (.db ou backup .tsv) vers session_history. Idempotent.

    Rapport : entrées importées / doublons ignorés / filtrées par durée
    minimale / erreurs (+ lignes mal formées pour un .tsv).
    """
    rows, malformed, source_format = load_source(path)

    # Résolution des noms d'utilisateurs : API Jellyfin (/Users) si
    # configurée, sinon cache local.
    usernames: dict[str, str] = {
        u["jellyfin_user_id"]: u["username"]
        for u in database.query("SELECT jellyfin_user_id, username FROM users")
    }
    try:
        for jf_user in api.get_users():
            usernames[jf_user["Id"]] = jf_user.get("Name", jf_user["Id"])
    except JellyfinError as exc:
        logger.warning("Résolution des utilisateurs via Jellyfin impossible : %s", exc)

    # Enrichissement depuis le cache items (runtime → percent, genres, série).
    items = {
        i["item_id"]: i
        for i in database.query(
            "SELECT item_id, library_id, series_name, season_number, episode_number,"
            " runtime_seconds, genres, video_resolution, video_codec, audio_codec"
            " FROM items"
        )
    }
    library_names = {
        l["library_id"]: l["name"]
        for l in database.query("SELECT library_id, name FROM libraries")
    }

    report = {"imported": 0, "duplicates": 0, "filtered": 0,
              "errors": malformed, "format": source_format}

    with database.db() as conn:
        for row in rows:
            try:
                started_at = _normalize_date(row["DateCreated"])
                duration = int(float(row["PlayDuration"] or 0))
                user_id = row["UserId"] or ""
                item = items.get(row["ItemId"], {})
                runtime = item.get("runtime_seconds")

                record, reason = should_record(
                    duration, runtime, minimum_duration, minimum_percent)
                if not record:
                    logger.debug("Import : entrée filtrée (%s, %s) : %s",
                                 row["ItemName"], started_at, reason)
                    report["filtered"] += 1
                    continue

                percent = (min(100.0, duration / runtime * 100)
                           if runtime else None)
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO session_history
                        (source, jellyfin_user_id, user_name, item_id, item_type,
                         item_name, series_name, season_number, episode_number,
                         library_id, library_name, started_at, play_duration,
                         runtime_seconds, percent_complete, client_name,
                         device_name, play_method, video_resolution, video_codec,
                         audio_codec, genres)
                    VALUES ('import', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        usernames.get(user_id, user_id[:8] if user_id else "?"),
                        row["ItemId"], row["ItemType"], row["ItemName"],
                        item.get("series_name"), item.get("season_number"),
                        item.get("episode_number"), item.get("library_id"),
                        library_names.get(item.get("library_id")),
                        started_at, duration, runtime, percent,
                        row["ClientName"], row["DeviceName"], row["PlaybackMethod"],
                        item.get("video_resolution"), item.get("video_codec"),
                        item.get("audio_codec"), item.get("genres"),
                    ),
                )
                if cursor.rowcount:
                    report["imported"] += 1
                else:
                    report["duplicates"] += 1
            except (sqlite3.Error, ValueError, KeyError) as exc:
                logger.warning("Import : entrée en erreur (%r) : %s", dict(row), exc)
                report["errors"] += 1

    logger.info(
        "Import Playback Reporting (%(format)s) terminé : %(imported)d importées, "
        "%(duplicates)d doublons, %(filtered)d filtrées, %(errors)d erreurs",
        report,
    )
    return report
