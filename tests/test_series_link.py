import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jellyfin_stats import database, graphs, history, scheduler

ETAG = "etag-1"


class EpisodeAPI:
    """Une bibliothèque avec une série et un épisode qui porte son SeriesId."""

    def get_users(self):
        return []

    def get_libraries(self):
        return [{"ItemId": "lib-1", "Name": "Séries", "CollectionType": "tvshows"}]

    def iter_item_inventory(self, parent_id):
        yield {"Id": "series-b", "Etag": ETAG}
        yield {"Id": "ep-1", "Etag": ETAG}

    def iter_items_by_ids(self, item_ids):
        rich = {
            "series-b": {"Id": "series-b", "Name": "The Office", "Type": "Series", "Etag": ETAG},
            "ep-1": {
                "Id": "ep-1",
                "Name": "Pilot",
                "Type": "Episode",
                "SeriesName": "The Office",
                "SeriesId": "series-b",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
                "Etag": ETAG,
            },
        }
        for item_id in item_ids:
            yield dict(rich[item_id])


class SeriesLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        database.init(str(Path(self.tmp.name) / "tautufin.db"))
        scheduler._sync_set(running=False, error=None)

    def tearDown(self):
        scheduler._sync_set(running=False, error=None)
        self.tmp.cleanup()

    def _play(self, item_id, series_name, series_id=None, started="2026-11-01 20:00:00"):
        database.execute(
            """
            INSERT INTO session_history
                (jellyfin_user_id, user_name, item_id, item_type, item_name,
                 series_name, series_id, started_at, play_duration)
            VALUES ('u1', 'Alice', ?, 'Episode', 'Pilot', ?, ?, ?, 1200)
            """,
            (item_id, series_name, series_id, started),
        )

    def _sync(self):
        now = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)
        with patch("jellyfin_stats.scheduler._utc_now", return_value=now):
            return scheduler.sync_all(EpisodeAPI(), force_full=True)

    def test_sync_stores_series_id_and_links_existing_plays(self):
        self._play("ep-1", "The Office")
        self._play("gone", "Ancienne série")  # média absent du catalogue

        self._sync()

        self.assertEqual(
            database.query_one("SELECT series_id FROM items WHERE item_id = 'ep-1'"),
            {"series_id": "series-b"},
        )
        self.assertEqual(
            database.query("SELECT item_id, series_id FROM session_history ORDER BY item_id"),
            [
                {"item_id": "ep-1", "series_id": "series-b"},
                {"item_id": "gone", "series_id": None},
            ],
        )

    def test_backfill_is_idempotent_and_keeps_existing_links(self):
        database.execute(
            "INSERT INTO items (item_id, type, series_id) VALUES ('ep-1', 'Episode', 'series-b')"
        )
        self._play("ep-1", "The Office")
        self._play("ep-1", "The Office", series_id="déjà-lié", started="2026-11-02 20:00:00")

        self.assertEqual(database.backfill_history_series(), 1)
        self.assertEqual(database.backfill_history_series(), 0)
        self.assertEqual(
            [r["series_id"] for r in database.query("SELECT series_id FROM session_history")],
            ["series-b", "déjà-lié"],
        )

    def test_readers_prefer_series_id_over_an_ambiguous_name(self):
        database.execute(
            "INSERT INTO items (item_id, type, name) VALUES "
            "('series-a', 'Series', 'The Office'), ('series-b', 'Series', 'The Office')"
        )
        self._play("ep-1", "The Office", series_id="series-b")

        row = history.get_history()["rows"][0]
        recent = graphs.recently_watched()[0]

        self.assertEqual(row["image_id"], "series-b")
        self.assertEqual(recent["image_id"], "series-b")

    def test_unlinked_play_still_falls_back_to_the_series_name(self):
        database.execute(
            "INSERT INTO items (item_id, type, name) VALUES ('series-a', 'Series', 'Archer')"
        )
        self._play("old-ep", "Archer")

        self.assertEqual(history.get_history()["rows"][0]["image_id"], "series-a")
        self.assertEqual(graphs.recently_watched()[0]["image_id"], "series-a")


if __name__ == "__main__":
    unittest.main()
