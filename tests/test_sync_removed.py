import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jellyfin_stats import database, libraries, scheduler

ETAG = "etag-1"


def movie(item_id, name=None):
    return {"Id": item_id, "Name": name or item_id, "Type": "Movie", "Etag": ETAG}


class LibraryAPI:
    def __init__(self, libraries_items, fail_on=None):
        self.libraries_items = libraries_items  # {library_id: [item_id, ...]}
        self.fail_on = fail_on
        self.rich_calls = []

    def get_users(self):
        return []

    def get_libraries(self):
        return [
            {"ItemId": lib, "Name": lib, "CollectionType": "movies"} for lib in self.libraries_items
        ]

    def iter_item_inventory(self, parent_id):
        if parent_id == self.fail_on:
            raise RuntimeError("inventaire indisponible")
        for item_id in self.libraries_items[parent_id]:
            yield {"Id": item_id, "Etag": ETAG}

    def iter_items_by_ids(self, item_ids):
        ids = list(item_ids)
        self.rich_calls.append(ids)
        for item_id in ids:
            yield movie(item_id)


class RemovedItemsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        database.init(str(Path(self.tmp.name) / "tautufin.db"))
        scheduler._sync_set(running=False, error=None)

    def tearDown(self):
        scheduler._sync_set(running=False, error=None)
        self.tmp.cleanup()

    def _sync(self, api):
        now = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)
        with patch("jellyfin_stats.scheduler._utc_now", return_value=now):
            return scheduler.sync_all(api)

    def _removed(self):
        return {
            r["item_id"]: (r["library_id"], r["removed_at"] is not None)
            for r in database.query("SELECT item_id, library_id, removed_at FROM items")
        }

    def test_media_missing_from_inventory_is_hidden_from_catalog_not_from_history(self):
        self._sync(LibraryAPI({"films": ["a", "b"]}))
        database.execute(
            "INSERT INTO session_history (jellyfin_user_id, item_id, item_type, started_at,"
            " play_duration) VALUES ('u1', 'b', 'Movie', '2026-11-01 20:00:00', 3600)"
        )

        self._sync(LibraryAPI({"films": ["a"]}))

        self.assertEqual(self._removed(), {"a": ("films", False), "b": ("films", True)})
        detail = libraries.library_detail("films")
        self.assertEqual([i["item_id"] for i in detail["catalog"]], ["a"])
        self.assertEqual([i["name"] for i in detail["most_watched"]], ["b"])

    def test_media_back_with_the_same_etag_is_restored_without_enrichment(self):
        self._sync(LibraryAPI({"films": ["a", "b"]}))
        self._sync(LibraryAPI({"films": ["a"]}))
        api = LibraryAPI({"films": ["a", "b"]})

        self._sync(api)

        self.assertEqual(api.rich_calls, [])
        self.assertEqual(self._removed(), {"a": ("films", False), "b": ("films", False)})

    def test_removed_media_reappearing_in_another_library_moves_there(self):
        self._sync(LibraryAPI({"films": ["a", "b"], "docs": []}))
        self._sync(LibraryAPI({"films": ["a"], "docs": []}))

        self._sync(LibraryAPI({"films": ["a"], "docs": ["b"]}))

        self.assertEqual(self._removed(), {"a": ("films", False), "b": ("docs", False)})

    def test_failed_inventory_marks_nothing_in_that_library(self):
        self._sync(LibraryAPI({"films": ["a"], "docs": ["d"]}))

        with self.assertRaises(RuntimeError):
            self._sync(LibraryAPI({"films": [], "docs": []}, fail_on="docs"))

        # « films » a été inventoriée avec succès avant l'échec : son marquage
        # est cohérent ; « docs » n'a pas pu l'être : rien n'y change.
        self.assertEqual(self._removed(), {"a": ("films", True), "d": ("docs", False)})


if __name__ == "__main__":
    unittest.main()
