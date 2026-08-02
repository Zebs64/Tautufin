import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jellyfin_stats import database, scheduler


REFRESH_1 = "2026-08-01T10:00:00.0000000Z"
REFRESH_2 = "2026-08-02T10:00:00.0000000Z"


def rich_item(item_id, marker=REFRESH_1, name=None):
    return {
        "Id": item_id,
        "Name": name or item_id,
        "Type": "Movie",
        "Genres": ["Drame"],
        "MediaStreams": [],
        "People": [],
        "Etag": marker,
    }


class FakeTwoPassAPI:
    def __init__(self, inventory, rich=None, *, fail_inventory=False, fail_rich=False):
        self.inventory = [dict(item) for item in inventory]
        self.rich = {item["Id"]: dict(item) for item in (rich or [])}
        self.fail_inventory = fail_inventory
        self.fail_rich = fail_rich
        self.inventory_calls = []
        self.rich_calls = []

    def get_users(self):
        return []

    def get_libraries(self):
        return [{"ItemId": "lib-1", "Name": "Films", "CollectionType": "movies"}]

    def iter_item_inventory(self, parent_id):
        self.inventory_calls.append(parent_id)
        if self.fail_inventory:
            raise RuntimeError("inventory failure")
        yield from [dict(item) for item in self.inventory]

    def iter_items_by_ids(self, item_ids):
        ids = list(item_ids)
        self.rich_calls.append(ids)
        if self.fail_rich:
            raise RuntimeError("rich failure")
        for item_id in ids:
            if item_id in self.rich:
                yield dict(self.rich[item_id])


class TwoPassSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tautufin.db")
        database.init(self.db_path)
        scheduler._sync_set(running=False, error=None)

    def tearDown(self):
        scheduler._sync_set(running=False, error=None)
        self.tmp.cleanup()

    def _seed_item(self, item_id, marker, name=None):
        database.execute(
            """
            INSERT INTO items
                (item_id, library_id, name, type, source_etag)
            VALUES (?, 'lib-1', ?, 'Movie', ?)
            """,
            (item_id, name or item_id, marker),
        )

    def _run(self, api, *, force_full=False, hour=11):
        now = datetime(2026, 8, 2, hour, 0, tzinfo=timezone.utc)
        with patch("jellyfin_stats.scheduler._utc_now", side_effect=[now, now]):
            return scheduler.sync_all(api, force_full=force_full)

    def test_large_unchanged_inventory_makes_no_rich_call(self):
        inventory = [
            {"Id": f"item-{index}", "Etag": REFRESH_1}
            for index in range(200)
        ]
        for item in inventory:
            self._seed_item(item["Id"], REFRESH_1)
        database.set_sync_cursor("2026-08-02T10:00:00Z")
        api = FakeTwoPassAPI(inventory)

        result = self._run(api)

        self.assertEqual(api.inventory_calls, ["lib-1"])
        self.assertEqual(api.rich_calls, [])
        self.assertEqual(result["items"], 200)
        self.assertEqual(result["items_inspected"], 200)
        self.assertEqual(result["items_enriched"], 0)
        self.assertEqual(result["items_changed"], 0)

    def test_only_item_with_changed_marker_is_enriched(self):
        self._seed_item("same", REFRESH_1)
        self._seed_item("changed", REFRESH_1, name="Ancien")
        inventory = [
            {"Id": "same", "Etag": REFRESH_1},
            {"Id": "changed", "Etag": REFRESH_2},
        ]
        api = FakeTwoPassAPI(
            inventory,
            [rich_item("changed", REFRESH_2, name="Nouveau")],
        )
        database.set_sync_cursor("2026-08-02T10:00:00Z")

        result = self._run(api)

        self.assertEqual(api.rich_calls, [["changed"]])
        self.assertEqual(result["items_enriched"], 1)
        self.assertEqual(result["items_changed"], 1)
        self.assertEqual(
            database.query_one(
                "SELECT name, source_etag FROM items WHERE item_id = 'changed'"
            ),
            {"name": "Nouveau", "source_etag": REFRESH_2},
        )

    def test_missing_item_is_enriched_and_inserted(self):
        api = FakeTwoPassAPI(
            [{"Id": "new", "Etag": REFRESH_1}],
            [rich_item("new")],
        )

        result = self._run(api)

        self.assertEqual(api.rich_calls, [["new"]])
        self.assertEqual(result["items_enriched"], 1)
        self.assertEqual(result["items_changed"], 1)
        self.assertEqual(
            database.query_one(
                "SELECT source_etag FROM items WHERE item_id = 'new'"
            ),
            {"source_etag": REFRESH_1},
        )

    def test_manual_full_sync_enriches_all_items_even_when_markers_match(self):
        self._seed_item("one", REFRESH_1)
        self._seed_item("two", REFRESH_1)
        inventory = [
            {"Id": "one", "Etag": REFRESH_1},
            {"Id": "two", "Etag": REFRESH_1},
        ]
        api = FakeTwoPassAPI(inventory, [rich_item("one"), rich_item("two")])

        result = self._run(api, force_full=True)

        self.assertEqual(api.rich_calls, [["one", "two"]])
        self.assertEqual(result["items_enriched"], 2)

    def test_network_failure_preserves_marker_cursor_and_previous_success(self):
        self._seed_item("changed", REFRESH_1, name="Ancien")
        previous_cursor = "2026-08-02T10:00:00Z"
        previous_success = "2026-08-02T10:00:01Z"
        database.set_sync_cursor(previous_cursor)
        database.set_sync_health({"status": "success", "last_success_at": previous_success})
        api = FakeTwoPassAPI(
            [{"Id": "changed", "Etag": REFRESH_2}],
            fail_rich=True,
        )

        with self.assertRaisesRegex(RuntimeError, "rich failure"):
            self._run(api)

        row = database.query_one(
            "SELECT name, source_etag FROM items WHERE item_id = 'changed'"
        )
        health = database.get_sync_health()
        self.assertEqual(row, {"name": "Ancien", "source_etag": REFRESH_1})
        self.assertEqual(database.get_sync_cursor(), previous_cursor)
        self.assertEqual(health["last_success_at"], previous_success)
        self.assertTrue(health["cursor_preserved"])

    def test_sqlite_failure_rolls_back_rich_data_and_marker(self):
        self._seed_item("changed", REFRESH_1, name="Ancien")
        previous_cursor = "2026-08-02T10:00:00Z"
        database.set_sync_cursor(previous_cursor)
        database.execute(
            """
            CREATE TRIGGER fail_marker_update
            BEFORE UPDATE OF source_etag ON items
            WHEN NEW.source_etag = '2026-08-02T10:00:00.0000000Z'
            BEGIN
                SELECT RAISE(ABORT, 'injected marker failure');
            END
            """
        )
        api = FakeTwoPassAPI(
            [{"Id": "changed", "Etag": REFRESH_2}],
            [rich_item("changed", REFRESH_2, name="Nouveau")],
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected marker failure"):
            self._run(api)

        self.assertEqual(
            database.query_one(
                "SELECT name, source_etag FROM items WHERE item_id = 'changed'"
            ),
            {"name": "Ancien", "source_etag": REFRESH_1},
        )
        self.assertEqual(database.get_sync_cursor(), previous_cursor)

    def test_missing_rich_response_does_not_validate_cursor_or_marker(self):
        previous_cursor = "2026-08-02T10:00:00Z"
        database.set_sync_cursor(previous_cursor)
        api = FakeTwoPassAPI([{"Id": "missing", "Etag": REFRESH_1}])

        with self.assertRaisesRegex(Exception, "missing"):
            self._run(api)

        self.assertIsNone(
            database.query_one("SELECT item_id FROM items WHERE item_id = 'missing'")
        )
        self.assertEqual(database.get_sync_cursor(), previous_cursor)


class DatabaseV6MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tautufin-v5.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_populated_v5(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (0)")
        for version, statements in database.MIGRATIONS:
            if version > 5:
                break
            for statement in statements:
                conn.execute(statement)
            conn.execute("UPDATE schema_version SET version = ?", (version,))
        conn.execute(
            "INSERT INTO items (item_id, library_id, name, type) VALUES (?, ?, ?, ?)",
            ("item-1", "lib-1", "Film conservé", "Movie"),
        )
        conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?)",
            ("items_cursor_utc", "2026-08-01T10:00:00Z"),
        )
        conn.commit()
        conn.close()

    def test_populated_v5_migrates_to_v7_without_loss_and_is_idempotent(self):
        self._create_populated_v5()

        database.init(self.db_path)
        database.init(self.db_path)

        self.assertEqual(
            database.query_one("SELECT version FROM schema_version"), {"version": 7}
        )
        self.assertEqual(
            database.query_one(
                "SELECT name, source_etag FROM items WHERE item_id = 'item-1'"
            ),
            {"name": "Film conservé", "source_etag": None},
        )
        self.assertEqual(database.get_sync_cursor(), "2026-08-01T10:00:00Z")
        self.assertTrue(database.integrity_check()["ok"])


if __name__ == "__main__":
    unittest.main()
