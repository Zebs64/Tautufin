import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jellyfin_stats import database, scheduler


class FakeSyncAPI:
    def __init__(self, items=None, fail=False):
        self.items = items or []
        self.fail = fail
        self.inventory_calls = []
        self.rich_calls = []

    def get_users(self):
        return []

    def get_libraries(self):
        return [{"ItemId": "lib-1", "Name": "Films", "CollectionType": "movies"}]

    def iter_item_inventory(self, parent_id):
        self.inventory_calls.append(parent_id)
        if self.fail:
            raise RuntimeError("Jellyfin failure")
        yield from [
            {"Id": item["Id"], "Etag": item.get("Etag")}
            for item in self.items
        ]

    def iter_items_by_ids(self, item_ids):
        ids = list(item_ids)
        self.rich_calls.append(ids)
        by_id = {item["Id"]: item for item in self.items}
        yield from [dict(by_id[item_id]) for item_id in ids]


class InlineThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


class IncrementalSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tautufin.db")
        database.init(self.db_path)
        scheduler._sync_set(running=False, error=None)

    def tearDown(self):
        scheduler._sync_set(running=False, error=None)
        self.tmp.cleanup()

    def test_missing_cursor_runs_full_and_success_stores_start_bound(self):
        api = FakeSyncAPI()
        started = datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc)

        with patch("jellyfin_stats.scheduler._utc_now", return_value=started):
            scheduler.sync_all(api)

        self.assertEqual(api.inventory_calls, ["lib-1"])
        self.assertEqual(database.get_sync_health()["mode"], "full")
        self.assertEqual(database.get_sync_cursor(), "2026-07-30T11:00:00Z")

    def test_existing_cursor_runs_incremental_inventory(self):
        database.set_sync_cursor("2026-07-30T10:00:00Z")
        api = FakeSyncAPI()
        started = datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc)

        with patch("jellyfin_stats.scheduler._utc_now", return_value=started):
            scheduler.sync_all(api)

        self.assertEqual(api.inventory_calls, ["lib-1"])
        self.assertEqual(database.get_sync_health()["mode"], "incremental")
        self.assertEqual(database.get_sync_cursor(), "2026-07-30T11:00:00Z")

    def test_invalid_cursor_falls_back_to_full(self):
        database.set_sync_cursor("not-a-date")
        api = FakeSyncAPI()

        with patch(
            "jellyfin_stats.scheduler._utc_now",
            return_value=datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc),
        ):
            scheduler.sync_all(api)

        self.assertEqual(api.inventory_calls, ["lib-1"])
        self.assertEqual(database.get_sync_health()["mode"], "full")

    def test_forced_full_omits_cursor_and_refreshes_it(self):
        database.set_sync_cursor("2026-07-30T10:00:00Z")
        api = FakeSyncAPI()

        with patch(
            "jellyfin_stats.scheduler._utc_now",
            return_value=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
        ):
            scheduler.sync_all(api, force_full=True)

        self.assertEqual(api.inventory_calls, ["lib-1"])
        self.assertEqual(database.get_sync_health()["mode"], "full")
        self.assertEqual(database.get_sync_cursor(), "2026-07-30T12:00:00Z")

    def test_partial_failure_preserves_previous_cursor(self):
        database.set_sync_cursor("2026-07-30T10:00:00Z")
        api = FakeSyncAPI(fail=True)

        with patch(
            "jellyfin_stats.scheduler._utc_now",
            return_value=datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc),
        ):
            with self.assertRaisesRegex(RuntimeError, "Jellyfin failure"):
                scheduler.sync_all(api)

        self.assertEqual(database.get_sync_cursor(), "2026-07-30T10:00:00Z")

    def test_overlap_replay_keeps_identical_media_timestamp(self):
        item = {
            "Id": "item-1",
            "Name": "Film",
            "Type": "Movie",
            "Genres": ["Drame"],
            "MediaStreams": [],
            "People": [],
            "Etag": "refresh-1",
        }
        api = FakeSyncAPI([item])
        bounds = [
            datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc),
        ]

        with patch("jellyfin_stats.scheduler._utc_now", side_effect=bounds):
            with patch("jellyfin_stats.scheduler.now_iso", return_value="first"):
                scheduler.sync_all(api)
            with patch("jellyfin_stats.scheduler.now_iso", return_value="second"):
                scheduler.sync_all(api)

        row = database.query_one(
            "SELECT updated_at FROM items WHERE item_id = 'item-1'"
        )
        self.assertEqual(api.inventory_calls, ["lib-1", "lib-1"])
        self.assertEqual(api.rich_calls, [["item-1"]])
        self.assertEqual(row, {"updated_at": "first"})

    def test_manual_start_forces_full_and_running_guard_is_preserved(self):
        calls = []

        def fake_sync_all(api, report=None, force_full=False):
            calls.append(force_full)
            return {"users": 0, "items": 0}

        with patch("jellyfin_stats.scheduler.sync_all", fake_sync_all):
            with patch("jellyfin_stats.scheduler.threading.Thread", InlineThread):
                self.assertTrue(scheduler.start_sync(object()))

        self.assertEqual(calls, [True])
        scheduler._sync_set(running=True)
        with patch("jellyfin_stats.scheduler.threading.Thread") as thread:
            self.assertFalse(scheduler.start_sync(object()))
            thread.assert_not_called()

    def test_periodic_scheduler_requests_automatic_mode(self):
        config = SimpleNamespace(
            jellyfin_configured=True,
            sync_interval=60,
            poll_interval=15,
        )
        instance = scheduler.Scheduler(config, object(), object())

        async def run_once():
            with patch("jellyfin_stats.scheduler.start_sync") as start:
                with patch(
                    "jellyfin_stats.scheduler.asyncio.sleep",
                    side_effect=asyncio.CancelledError,
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        await instance._sync_loop()
                start.assert_called_once_with(instance.api, force_full=False)

        asyncio.run(run_once())


if __name__ == "__main__":
    unittest.main()
