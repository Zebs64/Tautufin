"""Tâches planifiées (boucles asyncio, démarrées dans le lifespan FastAPI) :

- polling de /Sessions toutes les ``poll_interval`` secondes ;
- synchronisation utilisateurs / bibliothèques / médias toutes les
  ``sync_interval`` secondes ;
- purge horaire des sessions HTTP expirées.
"""

import asyncio
import json
import logging

from . import auth, database
from .activity import TICKS_PER_SECOND, resolution_label
from .database import now_iso
from .jellyfin_api import JellyfinError

logger = logging.getLogger(__name__)


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


def sync_libraries_and_items(api) -> int:
    total_items = 0
    for folder in api.get_libraries():
        library_id = folder.get("ItemId")
        if not library_id:
            continue
        count = 0
        with database.db() as conn:
            for item in api.iter_items(library_id):
                count += 1
                runtime_ticks = item.get("RunTimeTicks")
                video = next((s for s in item.get("MediaStreams", [])
                              if s.get("Type") == "Video"), {})
                audio = next((s for s in item.get("MediaStreams", [])
                              if s.get("Type") == "Audio"), {})
                conn.execute(
                    """
                    INSERT INTO items
                        (item_id, library_id, name, type, series_name,
                         season_number, episode_number, year, genres,
                         runtime_seconds, video_resolution, video_codec,
                         audio_codec, added_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        updated_at = excluded.updated_at
                    """,
                    (
                        item["Id"], library_id, item.get("Name"), item.get("Type"),
                        item.get("SeriesName"), item.get("ParentIndexNumber"),
                        item.get("IndexNumber"), item.get("ProductionYear"),
                        json.dumps(item["Genres"]) if item.get("Genres") else None,
                        runtime_ticks // TICKS_PER_SECOND if runtime_ticks else None,
                        resolution_label(video.get("Width"), video.get("Height")),
                        video.get("Codec"), audio.get("Codec"),
                        (item.get("DateCreated") or "").replace("T", " ")[:19] or None,
                        now_iso(),
                    ),
                )
            conn.execute(
                """
                INSERT INTO libraries (library_id, name, collection_type, item_count, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(library_id) DO UPDATE SET
                    name = excluded.name,
                    collection_type = excluded.collection_type,
                    item_count = excluded.item_count,
                    updated_at = excluded.updated_at
                """,
                (library_id, folder.get("Name", "?"),
                 folder.get("CollectionType"), count, now_iso()),
            )
        total_items += count
    return total_items


def sync_all(api) -> dict:
    users = sync_users(api)
    items = sync_libraries_and_items(api)
    logger.info("Synchronisation Jellyfin : %d utilisateurs, %d médias", users, items)
    return {"users": users, "items": items}


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
                try:
                    await asyncio.to_thread(sync_all, self.api)
                except JellyfinError as exc:
                    logger.warning("Synchronisation impossible : %s", exc)
                except Exception:
                    logger.exception("Erreur inattendue de la synchronisation")
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
