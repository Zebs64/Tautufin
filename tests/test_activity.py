import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jellyfin_stats import database
from jellyfin_stats.activity import TICKS_PER_SECOND, ActivityMonitor, _wall_iso
from jellyfin_stats.scheduler import Scheduler

T0 = 1_780_000_000.0


def playing(session_id="s1", item_id="i1", paused=False):
    return {
        "Id": session_id,
        "UserId": "u1",
        "UserName": "Alice",
        "Client": "Jellyfin Web",
        "DeviceName": "PC",
        "NowPlayingItem": {
            "Id": item_id,
            "Name": "Film",
            "Type": "Movie",
            "RunTimeTicks": 7200 * TICKS_PER_SECOND,
        },
        "PlayState": {"IsPaused": paused, "PositionTicks": 0, "PlayMethod": "DirectPlay"},
    }


class FakeAPI:
    def __init__(self):
        self.sessions = []

    def get_sessions(self):
        return list(self.sessions)


class ActivityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tautufin.db")
        database.init(self.db_path)
        self.config = SimpleNamespace(
            poll_interval=15,
            session_grace=90,
            minimum_duration=0,
            minimum_percent=0,
            jellyfin_configured=True,
        )
        self.api = FakeAPI()
        self.clock = T0
        clock = patch("jellyfin_stats.activity.time.time", side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _poll(self, monitor, at):
        self.clock = at
        monitor.poll(self.api)

    def _history(self):
        return database.query(
            "SELECT session_key, started_at, stopped_at, play_duration FROM session_history"
        )

    def _active_keys(self):
        return [r["session_key"] for r in database.query("SELECT session_key FROM active_sessions")]

    def test_playback_running_across_a_crash_is_resumed_not_split(self):
        before = ActivityMonitor(self.config)
        self.api.sessions = [playing()]
        for offset in (0, 15, 30):
            self._poll(before, T0 + offset)
        self.assertEqual(self._active_keys(), ["s1|i1"])

        # Crash : aucune finalisation, nouvelle instance sur la même base.
        after = ActivityMonitor(self.config)
        self.assertEqual(after.restore(), 1)
        self._poll(after, T0 + 45)
        self.api.sessions = []
        self._poll(after, T0 + 45 + 90)

        history = self._history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["play_duration"], 45)
        self.assertEqual(history[0]["stopped_at"], _wall_iso(T0 + 45))
        self.assertEqual(self._active_keys(), [])

    def test_playback_ended_during_downtime_uses_last_seen_as_stop(self):
        before = ActivityMonitor(self.config)
        self.api.sessions = [playing()]
        self._poll(before, T0)
        self._poll(before, T0 + 15)

        after = ActivityMonitor(self.config)
        after.restore()
        self.api.sessions = []
        self._poll(after, T0 + 3600)

        history = self._history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["play_duration"], 15)
        self.assertEqual(history[0]["stopped_at"], _wall_iso(T0 + 15))
        self.assertEqual(self._active_keys(), [])

    def test_scheduler_stop_keeps_running_playback_instead_of_finalizing(self):
        monitor = ActivityMonitor(self.config)
        self.api.sessions = [playing()]
        self._poll(monitor, T0)
        self._poll(monitor, T0 + 15)

        asyncio.run(Scheduler(self.config, self.api, monitor).stop())

        self.assertEqual(self._history(), [])
        self.assertEqual(self._active_keys(), ["s1|i1"])

    def test_session_below_thresholds_is_dropped_with_its_active_row(self):
        self.config.minimum_duration = 300
        monitor = ActivityMonitor(self.config)
        self.api.sessions = [playing()]
        self._poll(monitor, T0)
        self._poll(monitor, T0 + 15)
        self.api.sessions = []
        self._poll(monitor, T0 + 15 + 90)

        self.assertEqual(self._history(), [])
        self.assertEqual(self._active_keys(), [])

    def test_failed_finalization_keeps_the_session_for_the_next_cycle(self):
        monitor = ActivityMonitor(self.config)
        self.api.sessions = [playing()]
        self._poll(monitor, T0)
        self._poll(monitor, T0 + 15)
        database.execute(
            "CREATE TRIGGER fail_history BEFORE INSERT ON session_history "
            "BEGIN SELECT RAISE(ABORT, 'base verrouillée'); END"
        )
        self.api.sessions = []

        with self.assertLogs("jellyfin_stats.activity", level="ERROR"):
            self._poll(monitor, T0 + 15 + 90)
        self.assertEqual(self._active_keys(), ["s1|i1"])
        self.assertEqual(len(monitor.snapshot()), 1)

        database.execute("DROP TRIGGER fail_history")
        self._poll(monitor, T0 + 15 + 105)

        self.assertEqual(len(self._history()), 1)
        self.assertEqual(self._active_keys(), [])

    def test_webhook_stop_records_and_forgets_the_session(self):
        monitor = ActivityMonitor(self.config)
        self.api.sessions = [playing()]
        self._poll(monitor, T0)
        self._poll(monitor, T0 + 15)

        monitor.handle_webhook({"NotificationType": "PlaybackStop", "UserId": "u1", "ItemId": "i1"})

        self.assertEqual(len(self._history()), 1)
        self.assertEqual(monitor.snapshot(), [])
        self.assertEqual(self._active_keys(), [])

    def test_clock_going_backwards_never_removes_watched_time(self):
        monitor = ActivityMonitor(self.config)
        self.api.sessions = [playing()]
        self._poll(monitor, T0)
        self._poll(monitor, T0 + 15)
        self._poll(monitor, T0 - 600)

        self.assertEqual(monitor.snapshot()[0]["watched_seconds"], 15)

    def test_unreadable_rows_are_dropped_on_restore(self):
        good = ActivityMonitor(self.config)
        self.api.sessions = [playing()]
        self._poll(good, T0)
        with database.db() as conn:
            conn.executemany(
                "INSERT INTO active_sessions (session_key, state, updated_at) VALUES (?, ?, 'x')",
                [
                    ("bad-json", "not json"),
                    ("bad-shape", "[1, 2]"),
                    ("incomplete", '{"key": "incomplete"}'),
                    ("mismatch", database.query_one("SELECT state FROM active_sessions")["state"]),
                ],
            )

        with self.assertLogs("jellyfin_stats.activity", level="WARNING"):
            restored = ActivityMonitor(self.config).restore()

        self.assertEqual(restored, 1)
        self.assertEqual(self._active_keys(), ["s1|i1"])

    def test_persistence_failure_is_logged_and_retried(self):
        monitor = ActivityMonitor(self.config)
        self.api.sessions = [playing()]
        with patch.object(monitor, "persist"):
            self._poll(monitor, T0)

        with patch("jellyfin_stats.activity.database.db", side_effect=sqlite3.OperationalError):
            with self.assertLogs("jellyfin_stats.activity", level="WARNING"):
                monitor.persist()
        self.assertEqual(self._active_keys(), [])

        monitor.persist()

        self.assertEqual(self._active_keys(), ["s1|i1"])

    def test_live_episode_records_its_series_id(self):
        monitor = ActivityMonitor(self.config)
        episode = playing(item_id="ep-1")
        episode["NowPlayingItem"].update(
            Type="Episode", SeriesId="series-b", SeriesName="The Office"
        )
        self.api.sessions = [episode]
        self._poll(monitor, T0)
        self._poll(monitor, T0 + 15)
        self.api.sessions = []
        self._poll(monitor, T0 + 15 + 90)

        self.assertEqual(
            database.query_one("SELECT series_id, series_name FROM session_history"),
            {"series_id": "series-b", "series_name": "The Office"},
        )


if __name__ == "__main__":
    unittest.main()
