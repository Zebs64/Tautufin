"""Inférence d'historique depuis le statut « Lu » de Jellyfin.

Pour les visionnages antérieurs à la mise en service de Tautufin (ou survenus
pendant un arrêt du conteneur), le polling de ``/Sessions`` n'a rien capturé.
Jellyfin conserve néanmoins, par utilisateur et par média, un champ ``UserData``
(``Played``, ``LastPlayedDate``, ``PlayCount``). On peut donc reconstituer une
session « inférée » par média marqué lu : date = ``LastPlayedDate``, durée =
durée totale du média (hypothèse 100 % vu). Ni le client, ni le transcodage, ni
les éventuels visionnages multiples ne sont récupérables — Jellyfin ne mémorise
que la dernière lecture.

Source ``'infer'`` (3ᵉ valeur de ``session_history.source``, à côté de
``'live'`` et ``'import'``), ce qui rend ces sessions repérables et purgeables.

Déduplication (exigence : pas de doublon) :
- entre exécutions : chaque lancement **purge d'abord** les sessions inférées du
  périmètre traité puis les recrée → idempotent, jamais d'accumulation (utile
  quand ``LastPlayedDate`` évolue après un nouveau visionnage) ;
- vis-à-vis des sessions réelles ('live'/'import') : avant d'insérer, on vérifie
  qu'aucune session non inférée du même (utilisateur, média) n'existe à ±24 h de
  ``LastPlayedDate`` — on ne double pas une lecture déjà enregistrée ;
- filet de sécurité : l'index UNIQUE (user, item, started_at) + INSERT OR IGNORE.
"""

import json
import logging

from . import database
from .activity import TICKS_PER_SECOND, resolution_label, should_record
from .import_playback import _normalize_date
from .jellyfin_api import JellyfinError

logger = logging.getLogger(__name__)

# Types de médias pour lesquels le statut « Lu » de Jellyfin est fiable.
INFER_ITEM_TYPES = "Movie,Episode,Audio,AudioBook,MusicVideo"


def count_inferred(user_id: str | None = None) -> int:
    if user_id:
        row = database.query_one(
            "SELECT COUNT(*) AS n FROM session_history"
            " WHERE source = 'infer' AND jellyfin_user_id = ?", (user_id,))
    else:
        row = database.query_one(
            "SELECT COUNT(*) AS n FROM session_history WHERE source = 'infer'")
    return row["n"] if row else 0


def delete_inferred(user_id: str | None = None) -> int:
    """Supprime les sessions inférées (toutes, ou d'un utilisateur)."""
    if user_id:
        return database.execute(
            "DELETE FROM session_history WHERE source = 'infer'"
            " AND jellyfin_user_id = ?", (user_id,))
    return database.execute("DELETE FROM session_history WHERE source = 'infer'")


def _target_users(user_id: str | None) -> list[dict]:
    """Utilisateurs à traiter (cache local /Users). Restreint à un seul si
    ``user_id`` est fourni."""
    if user_id:
        rows = database.query(
            "SELECT jellyfin_user_id, username FROM users WHERE jellyfin_user_id = ?",
            (user_id,))
        # Utilisateur absent du cache mais demandé explicitement : on tente
        # quand même (l'API Jellyfin tranchera).
        return rows or [{"jellyfin_user_id": user_id, "username": user_id[:8]}]
    return database.query(
        "SELECT jellyfin_user_id, username FROM users ORDER BY username")


def _items_cache() -> dict[str, dict]:
    rows = database.query(
        """
        SELECT i.item_id, i.library_id, l.name AS library_name, i.series_name,
               i.season_number, i.episode_number, i.runtime_seconds, i.genres,
               i.video_resolution, i.video_codec, i.audio_codec
        FROM items i LEFT JOIN libraries l ON l.library_id = i.library_id
        """)
    return {r["item_id"]: r for r in rows}


def infer_history(api, minimum_duration: int, minimum_percent: int,
                  user_id: str | None = None) -> dict:
    """Reconstitue les sessions inférées. Idempotent (voir docstring module).

    Rapport : utilisateurs traités, sessions créées, doublons ignorés (lecture
    réelle déjà présente), filtrées (durée minimale), sans date, erreurs.
    """
    users = _target_users(user_id)
    items = _items_cache()
    report = {"users": 0, "created": 0, "duplicates": 0, "filtered": 0,
              "no_date": 0, "errors": 0, "deleted": 0}

    # Phase réseau : on récupère les médias lus de chaque utilisateur SANS
    # tenir de transaction SQLite (sinon le verrou d'écriture bloquerait le
    # poller pendant tous les appels HTTP).
    fetched: list[tuple[dict, list[dict]]] = []
    for user in users:
        try:
            played = list(api.iter_played_items(
                user["jellyfin_user_id"], item_types=INFER_ITEM_TYPES))
        except JellyfinError as exc:
            logger.warning("Inférence : médias lus de %s inaccessibles : %s",
                           user["username"], exc)
            report["errors"] += 1
            continue
        fetched.append((user, played))
        report["users"] += 1

    # Phase écriture : purge des inférées du périmètre puis recréation, dans une
    # transaction unique et courte (un échec laisse l'état d'origine intact).
    with database.db() as conn:
        if user_id:
            report["deleted"] = conn.execute(
                "DELETE FROM session_history WHERE source = 'infer'"
                " AND jellyfin_user_id = ?", (user_id,)).rowcount
        else:
            report["deleted"] = conn.execute(
                "DELETE FROM session_history WHERE source = 'infer'").rowcount

        for user, played in fetched:
            uid = user["jellyfin_user_id"]
            uname = user["username"]
            for item in played:
                try:
                    user_data = item.get("UserData") or {}
                    if not user_data.get("Played"):
                        continue
                    last_played = user_data.get("LastPlayedDate")
                    if not last_played:
                        report["no_date"] += 1
                        continue
                    started_at = _normalize_date(last_played)
                    item_id = item["Id"]
                    cached = items.get(item_id, {})

                    runtime_ticks = item.get("RunTimeTicks")
                    runtime_seconds = (runtime_ticks // TICKS_PER_SECOND
                                       if runtime_ticks
                                       else cached.get("runtime_seconds"))
                    # Hypothèse : un média « Lu » a été vu en entier.
                    play_duration = int(runtime_seconds or 0)

                    record, reason = should_record(
                        play_duration, runtime_seconds,
                        minimum_duration, minimum_percent)
                    if not record:
                        report["filtered"] += 1
                        continue

                    # Doublon d'une lecture réelle déjà enregistrée (±24 h) ?
                    dup = conn.execute(
                        """
                        SELECT 1 FROM session_history
                        WHERE jellyfin_user_id = ? AND item_id = ?
                          AND source != 'infer'
                          AND ABS(julianday(started_at) - julianday(?)) <= 1
                        LIMIT 1
                        """,
                        (uid, item_id, started_at)).fetchone()
                    if dup:
                        report["duplicates"] += 1
                        continue

                    video = next((s for s in item.get("MediaStreams", [])
                                  if s.get("Type") == "Video"), {})
                    audio = next((s for s in item.get("MediaStreams", [])
                                  if s.get("Type") == "Audio"), {})
                    genres = (json.dumps(item["Genres"]) if item.get("Genres")
                              else cached.get("genres"))
                    percent = 100.0 if runtime_seconds else None

                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO session_history
                            (session_key, source, jellyfin_user_id, user_name,
                             item_id, item_type, item_name, series_name,
                             season_number, episode_number, library_id,
                             library_name, started_at, stopped_at, play_duration,
                             runtime_seconds, percent_complete, video_resolution,
                             video_codec, audio_codec, genres)
                        VALUES (?, 'infer', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"infer:{uid}:{item_id}:{started_at}",
                            uid, uname, item_id, item.get("Type"),
                            item.get("Name"),
                            item.get("SeriesName") or cached.get("series_name"),
                            item.get("ParentIndexNumber") or cached.get("season_number"),
                            item.get("IndexNumber") or cached.get("episode_number"),
                            cached.get("library_id"), cached.get("library_name"),
                            started_at, started_at, play_duration,
                            runtime_seconds, percent,
                            resolution_label(video.get("Width"), video.get("Height"))
                            or cached.get("video_resolution"),
                            video.get("Codec") or cached.get("video_codec"),
                            audio.get("Codec") or cached.get("audio_codec"),
                            genres,
                        ),
                    )
                    if cursor.rowcount:
                        report["created"] += 1
                    else:
                        report["duplicates"] += 1
                except Exception:
                    logger.exception("Inférence : média en erreur (%s)",
                                     item.get("Name"))
                    report["errors"] += 1

    logger.info(
        "Inférence terminée : %(users)d utilisateurs, %(created)d créées, "
        "%(duplicates)d doublons, %(filtered)d filtrées, %(no_date)d sans date, "
        "%(errors)d erreurs (%(deleted)d inférées purgées avant)",
        report,
    )
    return report
