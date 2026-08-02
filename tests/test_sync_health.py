import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jellyfin_stats import database, scheduler


class FakeHealthAPI:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.inventory_calls = []
        self.rich_calls = []

    def get_users(self):
        return [
            {"Id": "user-1", "Name": "Alice", "Policy": {}},
            {"Id": "user-2", "Name": "Bob", "Policy": {}},
        ]

    def get_libraries(self):
        return [
            {"ItemId": "lib-1", "Name": "Films", "CollectionType": "movies"},
            {"ItemId": "lib-2", "Name": "Séries", "CollectionType": "tvshows"},
        ]

    def iter_item_inventory(self, parent_id):
        self.inventory_calls.append(parent_id)
        if self.fail:
            raise RuntimeError(
                "GET https://jellyfin.private/Items?api_key=super-secret "
                "token=other-secret\n" + "détail " * 100
            )
        yield {"Id": f"item-{parent_id}", "DateLastRefreshed": "refresh-1"}

    def iter_items_by_ids(self, item_ids):
        ids = list(item_ids)
        self.rich_calls.append(ids)
        for item_id in ids:
            parent_id = item_id.removeprefix("item-")
            yield {
                "Id": item_id,
                "Name": parent_id,
                "Type": "Movie",
                "Genres": [],
                "MediaStreams": [],
                "People": [],
                "DateLastRefreshed": "refresh-1",
            }


class SyncHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tautufin.db")
        database.init(self.db_path)
        scheduler._sync_set(running=False, error=None)

    def tearDown(self):
        scheduler._sync_set(running=False, error=None)
        self.tmp.cleanup()

    def test_absent_and_invalid_health_return_never_executed_state(self):
        expected = database.empty_sync_health()
        self.assertEqual(database.get_sync_health(), expected)

        database.execute(
            "INSERT INTO sync_state (key, value) VALUES ('sync_health', 'not-json')"
        )
        with self.assertLogs("jellyfin_stats.database", level="WARNING"):
            actual = database.get_sync_health()

        self.assertEqual(actual, expected)

    def test_full_success_persists_duration_and_exact_counters(self):
        api = FakeHealthAPI()
        times = [
            datetime(2026, 7, 31, 10, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 31, 10, 0, 3, 250000, tzinfo=timezone.utc),
        ]

        with patch("jellyfin_stats.scheduler._utc_now", side_effect=times):
            result = scheduler.sync_all(api, force_full=True)

        health = database.get_sync_health()
        self.assertEqual(result["users"], 2)
        self.assertEqual(result["libraries"], 2)
        self.assertEqual(result["items"], 2)
        self.assertEqual(result["items_changed"], 2)
        self.assertEqual(
            health,
            {
                "status": "success",
                "mode": "full",
                "phase": "done",
                "started_at": "2026-07-31T10:00:00Z",
                "last_attempt_at": "2026-07-31T10:00:00Z",
                "last_success_at": "2026-07-31T10:00:03Z",
                "finished_at": "2026-07-31T10:00:03Z",
                "duration_seconds": 3.25,
                "users": 2,
                "libraries": 2,
                "items_received": 2,
                "items_inspected": 2,
                "items_enriched": 2,
                "items_changed": 2,
                "error": None,
                "cursor_preserved": False,
            },
        )

    def test_incremental_success_uses_cursor_and_counts_unchanged_media(self):
        database.set_sync_cursor("2026-07-31T09:00:00Z")
        api = FakeHealthAPI()
        start = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)

        with patch("jellyfin_stats.scheduler._utc_now", side_effect=[start, start]):
            scheduler.sync_all(api, force_full=False)
        first_health = database.get_sync_health()

        api.inventory_calls.clear()
        api.rich_calls.clear()
        later = datetime(2026, 7, 31, 11, 0, tzinfo=timezone.utc)
        with patch("jellyfin_stats.scheduler._utc_now", side_effect=[later, later]):
            scheduler.sync_all(api, force_full=False)

        health = database.get_sync_health()
        self.assertEqual(first_health["mode"], "incremental")
        self.assertEqual(api.inventory_calls, ["lib-1", "lib-2"])
        self.assertEqual(api.rich_calls, [])
        self.assertEqual(health["items_received"], 2)
        self.assertEqual(health["items_inspected"], 2)
        self.assertEqual(health["items_enriched"], 0)
        self.assertEqual(health["items_changed"], 0)
        self.assertEqual(database.get_sync_cursor(), "2026-07-31T11:00:00Z")

    def test_failure_preserves_last_success_and_cursor_and_sanitizes_error(self):
        api = FakeHealthAPI()
        success_start = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
        success_end = datetime(2026, 7, 31, 10, 0, 1, tzinfo=timezone.utc)
        with patch(
            "jellyfin_stats.scheduler._utc_now",
            side_effect=[success_start, success_end],
        ):
            scheduler.sync_all(api, force_full=True)

        previous_cursor = database.get_sync_cursor()
        previous_success = database.get_sync_health()["last_success_at"]
        failed_api = FakeHealthAPI(fail=True)
        failure_start = datetime(2026, 7, 31, 11, 0, tzinfo=timezone.utc)
        failure_end = datetime(2026, 7, 31, 11, 0, 2, tzinfo=timezone.utc)
        with patch(
            "jellyfin_stats.scheduler._utc_now",
            side_effect=[failure_start, failure_end],
        ):
            with self.assertRaises(RuntimeError):
                scheduler.sync_all(failed_api, force_full=False)

        health = database.get_sync_health()
        self.assertEqual(health["status"], "error")
        self.assertEqual(health["last_success_at"], previous_success)
        self.assertEqual(database.get_sync_cursor(), previous_cursor)
        self.assertTrue(health["cursor_preserved"])
        self.assertLessEqual(len(health["error"]), 240)
        self.assertNotIn("super-secret", health["error"])
        self.assertNotIn("other-secret", health["error"])
        self.assertNotIn("jellyfin.private", health["error"])

    def test_final_health_failure_rolls_back_cursor_and_preserves_previous_success(self):
        previous_cursor = "2026-07-31T09:00:00Z"
        previous_success = "2026-07-31T09:00:01Z"
        database.set_sync_cursor(previous_cursor)
        database.set_sync_health({
            "status": "success",
            "last_success_at": previous_success,
        })
        database.execute(
            """
            CREATE TRIGGER fail_final_success_health
            BEFORE INSERT ON sync_state
            WHEN NEW.key = 'sync_health'
             AND instr(NEW.value, '\"status\":\"success\"') > 0
            BEGIN
                SELECT RAISE(ABORT, 'injected final health failure');
            END
            """
        )
        start = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 7, 31, 10, 0, 1, tzinfo=timezone.utc)

        with patch("jellyfin_stats.scheduler._utc_now", side_effect=[start, end, end]):
            with self.assertRaisesRegex(sqlite3.IntegrityError,
                                        "injected final health failure"):
                scheduler.sync_all(FakeHealthAPI(), force_full=False)

        health = database.get_sync_health()
        self.assertEqual(database.get_sync_cursor(), previous_cursor)
        self.assertEqual(health["last_success_at"], previous_success)
        self.assertEqual(health["status"], "error")
        self.assertEqual(health["phase"], "error")
        self.assertTrue(health["cursor_preserved"])
        self.assertIn("injected final health failure", health["error"])

    def test_health_survives_reinitialization_and_reset_clears_only_sync_keys(self):
        database.set_sync_cursor("2026-07-31T10:00:00Z")
        database.set_sync_health({"status": "success", "last_success_at": "saved"})
        database.execute(
            "INSERT INTO sync_state (key, value) VALUES ('protected-key', 'keep')"
        )

        database.init(self.db_path)
        self.assertEqual(database.get_sync_health()["last_success_at"], "saved")

        database.reset_data()

        self.assertIsNone(database.get_sync_cursor())
        self.assertEqual(database.get_sync_health(), database.empty_sync_health())
        self.assertEqual(
            database.query_one(
                "SELECT value FROM sync_state WHERE key = 'protected-key'"
            ),
            {"value": "keep"},
        )
        self.assertEqual(
            database.query_one("SELECT version FROM schema_version"), {"version": 6}
        )

    def test_status_combines_legacy_progress_with_persistent_health(self):
        database.set_sync_health({"status": "success", "last_success_at": "saved"})
        scheduler._sync_set(running=False, phase="done", users=2, items=3)

        status = scheduler.get_sync_status()

        self.assertFalse(status["running"])
        self.assertEqual(status["phase"], "done")
        self.assertEqual(status["users"], 2)
        self.assertEqual(status["items"], 3)
        self.assertEqual(status["health"]["last_success_at"], "saved")


if __name__ == "__main__":
    unittest.main()
