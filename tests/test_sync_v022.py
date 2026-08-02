import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jellyfin_stats import database, scheduler
from jellyfin_stats.jellyfin_api import JellyfinError


ETAG_1 = "2f44d6c1e79b4c63b1c7"
ETAG_2 = "8a972d45f30146df8ac0"


def rich_item(item_id, etag: str | None = ETAG_1, name=None):
    item = {
        "Id": item_id,
        "Name": name or item_id,
        "Type": "Movie",
        "Genres": ["Drame"],
        "MediaStreams": [],
        "People": [],
    }
    if etag is not None:
        item["Etag"] = etag
    return item


class Jellyfin101111API:
    """DTO shape observed on Jellyfin 10.11.11: no DateLastRefreshed field."""

    def __init__(self, inventory, rich=None):
        self.inventory = [dict(item) for item in inventory]
        self.rich = {item["Id"]: dict(item) for item in (rich or [])}
        self.inventory_calls = []
        self.rich_calls = []

    def get_users(self):
        return []

    def get_libraries(self):
        return [{"ItemId": "lib-1", "Name": "Films", "CollectionType": "movies"}]

    def iter_item_inventory(self, parent_id):
        self.inventory_calls.append(parent_id)
        yield from [dict(item) for item in self.inventory]

    def iter_items_by_ids(self, item_ids):
        ids = list(item_ids)
        self.rich_calls.append(ids)
        for item_id in ids:
            if item_id in self.rich:
                yield dict(self.rich[item_id])


class EtagSyncRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tautufin.db")
        database.init(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_item(self, item_id, etag, name=None):
        database.execute(
            """
            INSERT INTO items (item_id, library_id, name, type, source_etag)
            VALUES (?, 'lib-1', ?, 'Movie', ?)
            """,
            (item_id, name or item_id, etag),
        )

    def _run(self, api, *, force_full=False):
        now = datetime(2026, 8, 2, 11, 0, tzinfo=timezone.utc)
        with patch("jellyfin_stats.scheduler._utc_now", side_effect=[now, now]):
            return scheduler.sync_all(api, force_full=force_full)

    def test_200_identical_etags_make_no_rich_call(self):
        inventory = [{"Id": f"item-{index}", "Etag": ETAG_1} for index in range(200)]
        with database.db() as conn:
            conn.executemany(
                """
                INSERT INTO items (item_id, library_id, name, type, source_etag)
                VALUES (?, 'lib-1', ?, 'Movie', ?)
                """,
                [(item["Id"], item["Id"], ETAG_1) for item in inventory],
            )
        database.set_sync_cursor("2026-08-02T10:00:00Z")
        api = Jellyfin101111API(inventory)

        result = self._run(api)

        self.assertEqual(api.inventory_calls, ["lib-1"])
        self.assertEqual(api.rich_calls, [])
        self.assertEqual(result["items_inspected"], 200)
        self.assertEqual(result["items_enriched"], 0)
        self.assertEqual(result["items_changed"], 0)

    def test_only_changed_etag_is_enriched_and_persisted(self):
        self._seed_item("same", ETAG_1)
        self._seed_item("changed", ETAG_1, name="Ancien")
        api = Jellyfin101111API(
            [{"Id": "same", "Etag": ETAG_1}, {"Id": "changed", "Etag": ETAG_2}],
            [rich_item("changed", ETAG_2, name="Nouveau")],
        )
        database.set_sync_cursor("2026-08-02T10:00:00Z")

        result = self._run(api)

        self.assertEqual(api.rich_calls, [["changed"]])
        self.assertEqual(result["items_enriched"], 1)
        self.assertEqual(result["items_changed"], 1)
        self.assertEqual(
            database.query_one("SELECT name, source_etag FROM items WHERE item_id = 'changed'"),
            {"name": "Nouveau", "source_etag": ETAG_2},
        )

    def test_missing_item_and_null_local_etag_are_targeted(self):
        self._seed_item("null-marker", None)
        api = Jellyfin101111API(
            [{"Id": "null-marker", "Etag": ETAG_1}, {"Id": "new", "Etag": ETAG_1}],
            [rich_item("null-marker"), rich_item("new")],
        )

        result = self._run(api)

        self.assertEqual(api.rich_calls, [["null-marker", "new"]])
        self.assertEqual(result["items_enriched"], 2)
        self.assertEqual(
            database.query("SELECT item_id, source_etag FROM items ORDER BY item_id"),
            [
                {"item_id": "new", "source_etag": ETAG_1},
                {"item_id": "null-marker", "source_etag": ETAG_1},
            ],
        )

    def test_inventory_without_etag_fails_before_enrichment_and_preserves_cursor(self):
        previous_cursor = "2026-08-02T10:00:00Z"
        database.set_sync_cursor(previous_cursor)
        api = Jellyfin101111API([{"Id": "invalid"}])

        with self.assertRaisesRegex(JellyfinError, "ETag média absent"):
            self._run(api)

        self.assertEqual(api.rich_calls, [])
        self.assertEqual(database.get_sync_cursor(), previous_cursor)
        self.assertEqual(database.get_sync_health()["items_enriched"], 0)

    def test_inventory_without_id_fails_before_enrichment_and_preserves_cursor(self):
        previous_cursor = "2026-08-02T10:00:00Z"
        database.set_sync_cursor(previous_cursor)
        api = Jellyfin101111API([{"Etag": ETAG_1}])

        with self.assertRaisesRegex(JellyfinError, "identifiant média absent"):
            self._run(api)

        self.assertEqual(api.rich_calls, [])
        self.assertEqual(database.get_sync_cursor(), previous_cursor)

    def test_rich_item_without_etag_is_atomic(self):
        self._assert_invalid_rich_response(rich_item("changed", None), "ETag.*absent")

    def test_rich_item_with_divergent_etag_is_atomic(self):
        self._assert_invalid_rich_response(rich_item("changed", ETAG_1), "ETag.*divergent")

    def _assert_invalid_rich_response(self, rich, error_pattern):
        self._seed_item("changed", ETAG_1, name="Ancien")
        previous_cursor = "2026-08-02T10:00:00Z"
        database.set_sync_cursor(previous_cursor)
        api = Jellyfin101111API(
            [{"Id": "changed", "Etag": ETAG_2}],
            [rich],
        )

        with self.assertRaisesRegex(JellyfinError, error_pattern):
            self._run(api)

        self.assertEqual(
            database.query_one("SELECT name, source_etag FROM items WHERE item_id = 'changed'"),
            {"name": "Ancien", "source_etag": ETAG_1},
        )
        self.assertEqual(database.get_sync_cursor(), previous_cursor)

    def test_manual_full_sync_enriches_all_items(self):
        self._seed_item("one", ETAG_1)
        self._seed_item("two", ETAG_1)
        api = Jellyfin101111API(
            [{"Id": "one", "Etag": ETAG_1}, {"Id": "two", "Etag": ETAG_1}],
            [rich_item("one"), rich_item("two")],
        )

        result = self._run(api, force_full=True)

        self.assertEqual(api.rich_calls, [["one", "two"]])
        self.assertEqual(result["items_enriched"], 2)


class DatabaseV7MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tautufin-v6.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_populated_v6(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (0)")
        for version, statements in database.MIGRATIONS:
            if version > 6:
                break
            for statement in statements:
                conn.execute(statement)
            conn.execute("UPDATE schema_version SET version = ?", (version,))
        conn.execute(
            """
            INSERT INTO items
                (item_id, library_id, name, type, source_date_last_refreshed)
            VALUES ('item-1', 'lib-1', 'Film conservé', 'Movie', NULL)
            """
        )
        conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?)",
            ("items_cursor_utc", "2026-08-01T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?)",
            ("sync_health", '{"status":"success","items_enriched":12}'),
        )
        conn.commit()
        conn.close()

    def test_populated_v6_migrates_to_v7_without_loss_and_is_idempotent(self):
        self._create_populated_v6()

        database.init(self.db_path)
        database.init(self.db_path)

        self.assertEqual(database.query_one("SELECT version FROM schema_version"), {"version": 7})
        self.assertEqual(
            database.query_one(
                """
                SELECT name, source_date_last_refreshed, source_etag
                FROM items WHERE item_id = 'item-1'
                """
            ),
            {
                "name": "Film conservé",
                "source_date_last_refreshed": None,
                "source_etag": None,
            },
        )
        self.assertEqual(database.get_sync_cursor(), "2026-08-01T10:00:00Z")
        self.assertEqual(database.get_sync_health()["items_enriched"], 12)
        self.assertTrue(database.integrity_check()["ok"])


if __name__ == "__main__":
    unittest.main()
