import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jellyfin_stats import database, scheduler
from jellyfin_stats.jellyfin_api import JellyfinError

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
        inventory = [{"Id": f"item-{index}", "Etag": REFRESH_1} for index in range(200)]
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
            database.query_one("SELECT name, source_etag FROM items WHERE item_id = 'changed'"),
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
            database.query_one("SELECT source_etag FROM items WHERE item_id = 'new'"),
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

        row = database.query_one("SELECT name, source_etag FROM items WHERE item_id = 'changed'")
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
            database.query_one("SELECT name, source_etag FROM items WHERE item_id = 'changed'"),
            {"name": "Ancien", "source_etag": REFRESH_1},
        )
        self.assertEqual(database.get_sync_cursor(), previous_cursor)

    def test_missing_rich_response_does_not_validate_cursor_or_marker(self):
        previous_cursor = "2026-08-02T10:00:00Z"
        database.set_sync_cursor(previous_cursor)
        api = FakeTwoPassAPI([{"Id": "missing", "Etag": REFRESH_1}])

        with self.assertRaisesRegex(Exception, "missing"):
            self._run(api)

        self.assertIsNone(database.query_one("SELECT item_id FROM items WHERE item_id = 'missing'"))
        self.assertEqual(database.get_sync_cursor(), previous_cursor)


ETAG_1 = "2f44d6c1e79b4c63b1c7"
ETAG_2 = "8a972d45f30146df8ac0"


def etag_rich_item(item_id, etag: str | None = ETAG_1, name=None):
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
            [etag_rich_item("changed", ETAG_2, name="Nouveau")],
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
            [etag_rich_item("null-marker"), etag_rich_item("new")],
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
        self._assert_invalid_rich_response(etag_rich_item("changed", None), "ETag.*absent")

    def test_rich_item_with_divergent_etag_is_atomic(self):
        self._assert_invalid_rich_response(etag_rich_item("changed", ETAG_1), "ETag.*divergent")

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
            [etag_rich_item("one"), etag_rich_item("two")],
        )

        result = self._run(api, force_full=True)

        self.assertEqual(api.rich_calls, [["one", "two"]])
        self.assertEqual(result["items_enriched"], 2)


class FakeLibraryAPI:
    def __init__(self, item):
        self.item = item

    def get_libraries(self):
        return [{"ItemId": "lib-1", "Name": "Films", "CollectionType": "movies"}]

    def iter_item_inventory(self, parent_id):
        self.last_parent_id = parent_id
        yield {
            "Id": self.item["Id"],
            "Etag": self.item.get("Etag"),
        }

    def iter_items_by_ids(self, item_ids):
        self.last_item_ids = list(item_ids)
        yield dict(self.item)


class UnchangedMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tautufin.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_identical_media_does_not_change_updated_at_but_changed_media_does(self):
        database.init(self.db_path)
        item = {
            "Id": "item-1",
            "Name": None,
            "Type": "Movie",
            "Genres": None,
            "MediaStreams": [],
            "People": None,
            "Etag": "refresh-1",
        }
        api = FakeLibraryAPI(item)

        with patch("jellyfin_stats.scheduler.now_iso", side_effect=["first", "library-1"]):
            scheduler.sync_libraries_and_items(api)
        with patch("jellyfin_stats.scheduler.now_iso", side_effect=["second", "library-2"]):
            scheduler.sync_libraries_and_items(api)
        unchanged = database.query_one(
            "SELECT name, updated_at FROM items WHERE item_id = 'item-1'"
        )

        api.item["Name"] = "Film modifié"
        api.item["Etag"] = "refresh-2"
        with patch("jellyfin_stats.scheduler.now_iso", side_effect=["third", "library-3"]):
            scheduler.sync_libraries_and_items(api)
        changed = database.query_one("SELECT name, updated_at FROM items WHERE item_id = 'item-1'")

        self.assertEqual(unchanged, {"name": None, "updated_at": "first"})
        self.assertEqual(changed, {"name": "Film modifié", "updated_at": "third"})


if __name__ == "__main__":
    unittest.main()
