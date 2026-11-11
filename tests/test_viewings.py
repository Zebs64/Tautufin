import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jellyfin_stats import database, import_playback, infer_history, viewings
from jellyfin_stats.activity import TICKS_PER_SECOND, ActivityMonitor
from jellyfin_stats.viewings import Segment, assign


def seg(id, start, duration, percent=None, *, stop=None, runtime=6000, source="live", item="i1"):
    return Segment(id, "u1", item, start, stop, duration, runtime, percent, source)


class AssignRuleTests(unittest.TestCase):
    def test_live_resume_after_a_pause_is_one_viewing(self):
        groups = assign(
            [
                seg(1, "2026-11-01 20:00:00", 1800, 40.0, stop="2026-11-01 20:30:00"),
                seg(2, "2026-11-01 20:45:00", 3000, 97.0, stop="2026-11-01 21:35:00"),
            ]
        )
        self.assertEqual(groups, {1: 1, 2: 1})

    def test_finished_viewing_then_rewatch_is_two_viewings(self):
        groups = assign(
            [
                seg(1, "2026-11-01 20:00:00", 5800, 96.0, stop="2026-11-01 21:40:00"),
                seg(2, "2026-11-02 20:00:00", 5800, 96.0, stop="2026-11-02 21:40:00"),
            ]
        )
        self.assertEqual(groups, {1: 1, 2: 2})

    def test_gap_over_24_hours_starts_a_new_viewing(self):
        groups = assign(
            [
                seg(1, "2026-11-01 20:00:00", 1800, 30.0, stop="2026-11-01 20:30:00"),
                seg(2, "2026-11-02 20:31:00", 1800, 60.0, stop="2026-11-02 21:01:00"),
            ]
        )
        self.assertEqual(groups, {1: 1, 2: 2})

    def test_imports_use_the_cumulative_share_watched(self):
        # Pourcentage d'import = part vue du segment, pas une position.
        groups = assign(
            [
                seg(1, "2026-11-01 20:00:00", 1800, 30.0, source="import"),
                seg(2, "2026-11-02 10:00:00", 4200, 70.0, source="import"),  # 30 + 70 = fini
                seg(3, "2026-11-02 21:00:00", 6000, 100.0, source="import"),  # revisionnage
            ]
        )
        self.assertEqual(groups, {1: 1, 2: 1, 3: 3})

    def test_estimated_and_unknown_progress_never_merge(self):
        estimated = assign(
            [
                seg(1, "2026-11-01 20:00:00", 1800, 30.0, stop="2026-11-01 20:30:00"),
                seg(2, "2026-11-01 22:00:00", 6000, 100.0, source="infer"),
            ]
        )
        unknown = assign(
            [
                seg(1, "2026-11-01 20:00:00", 1800, None, runtime=None, source="import"),
                seg(2, "2026-11-01 21:00:00", 1800, None, runtime=None, source="import"),
            ]
        )
        self.assertEqual(estimated, {1: 1, 2: 2})
        self.assertEqual(unknown, {1: 1, 2: 2})

    def test_near_duplicate_segments_merge(self):
        groups = assign(
            [
                seg(1, "2026-11-01 20:00:00", 600, 10.0, stop="2026-11-01 20:10:00"),
                seg(2, "2026-11-01 20:00:30", 600, 10.0, stop="2026-11-01 20:10:30"),
            ]
        )
        self.assertEqual(groups, {1: 1, 2: 1})


class ViewingIdMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = str(self.root / "tautufin.db")
        database.init(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _insert(self, start, duration, percent, stop=None, source="live", item="i1"):
        database.execute(
            """
            INSERT INTO session_history (source, jellyfin_user_id, item_id, item_type,
                started_at, stopped_at, play_duration, runtime_seconds, percent_complete)
            VALUES (?, 'u1', ?, 'Movie', ?, ?, ?, 6000, ?)
            """,
            (source, item, start, stop, duration, percent),
        )

    def _viewings(self):
        return [
            r["viewing_id"]
            for r in database.query(
                "SELECT viewing_id FROM session_history ORDER BY started_at, id"
            )
        ]

    def _assert_consistent(self):
        broken = database.query_one(
            """
            SELECT COUNT(*) AS n FROM session_history h
            WHERE h.viewing_id IS NULL
               OR NOT EXISTS (SELECT 1 FROM session_history v WHERE v.id = h.viewing_id)
            """
        )
        self.assertEqual(broken["n"], 0)

    def test_startup_fills_missing_ids_and_recomputes_when_the_rule_changes(self):
        self._insert("2026-11-01 20:00:00", 1800, 40.0, "2026-11-01 20:30:00")
        self._insert("2026-11-01 20:45:00", 3000, 97.0, "2026-11-01 21:35:00")
        self.assertEqual(self._viewings(), [None, None])  # insertion directe

        database.init(self.db_path)
        self.assertEqual(self._viewings(), [1, 1])

        database.execute("UPDATE session_history SET viewing_id = id")  # état périmé
        with patch.object(viewings, "RULE_VERSION", "test-nouvelle-regle"):
            database.init(self.db_path)
        self.assertEqual(self._viewings(), [1, 1])

    def test_live_capture_links_a_resumed_playback(self):
        config = SimpleNamespace(
            poll_interval=15,
            session_grace=0,
            minimum_duration=0,
            minimum_percent=0,
            jellyfin_configured=True,
        )
        monitor = ActivityMonitor(config)
        clock = {"t": 1_780_000_000.0}

        def play(position):
            return [
                {
                    "Id": "s1",
                    "UserId": "u1",
                    "UserName": "Alice",
                    "NowPlayingItem": {
                        "Id": "i1",
                        "Name": "Film",
                        "Type": "Movie",
                        "RunTimeTicks": 6000 * TICKS_PER_SECOND,
                    },
                    "PlayState": {"PositionTicks": position * TICKS_PER_SECOND},
                }
            ]

        current = {"sessions": []}
        api = SimpleNamespace(get_sessions=lambda: current["sessions"])
        steps = [
            (play(600), 0),
            (play(1200), 600),
            ([], 60),  # arrêt à 20 %
            (play(1200), 3600),
            (play(5900), 600),
            ([], 60),  # reprise une heure plus tard
        ]
        wall = lambda: datetime.fromtimestamp(clock["t"]).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
        with (
            patch("jellyfin_stats.activity.time.time", side_effect=lambda: clock["t"]),
            patch("jellyfin_stats.activity.now_iso", side_effect=wall),
        ):
            for sessions, step in steps:
                clock["t"] += step
                current["sessions"] = sessions
                monitor.poll(api)

        ids = self._viewings()
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], ids[1])
        self._assert_consistent()

    def test_imported_segments_are_linked(self):
        path = self.root / "backup.tsv"
        lines = [
            "2026-11-01 20:00:00\tu1\ti1\tMovie\tFilm\tDirectPlay\tWeb\tPC\t1800",
            "2026-11-02 10:00:00\tu1\ti1\tMovie\tFilm\tDirectPlay\tWeb\tPC\t4200",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        database.execute(
            "INSERT INTO items (item_id, type, runtime_seconds) VALUES ('i1', 'Movie', 6000)"
        )

        import_playback.run_import(str(path), SimpleNamespace(get_users=lambda: []), 0, 0)

        ids = self._viewings()
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], ids[1])
        self._assert_consistent()

    def test_purging_inferred_plays_keeps_ids_consistent(self):
        self._insert("2026-11-01 20:00:00", 6000, 100.0, source="infer")
        self._insert("2026-11-03 20:00:00", 1800, 30.0, "2026-11-03 20:30:00")
        database.init(self.db_path)

        infer_history.delete_inferred()

        self.assertEqual(len(self._viewings()), 1)
        self._assert_consistent()


if __name__ == "__main__":
    unittest.main()
