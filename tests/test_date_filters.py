import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jellyfin_stats import clients, database, graphs, history, wrapped

BOUNDARY_PLAYS = [
    "2025-12-31 23:59:59",
    "2026-01-01 00:00:00",
    "2026-01-01 23:59:59",
    "2026-01-02 00:00:00",
    "2024-02-29 12:00:00",
]


class DateFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        database.init(str(Path(self.tmp.name) / "tautufin.db"))
        database.execute("INSERT INTO users (jellyfin_user_id, username) VALUES ('u1', 'Alice')")
        for index, started in enumerate(BOUNDARY_PLAYS):
            database.execute(
                """
                INSERT INTO session_history
                    (jellyfin_user_id, user_name, item_id, item_type, item_name,
                     started_at, play_duration, client_name, play_method)
                VALUES ('u1', 'Alice', ?, 'Movie', ?, ?, 600, 'Web', 'DirectPlay')
                """,
                (f"i{index}", f"Film {index}", started),
            )

    def tearDown(self):
        self.tmp.cleanup()

    def _history(self, date_from=None, date_to=None):
        rows = history.get_history(date_from=date_from, date_to=date_to, sort="date", order="asc")
        return [r["started_at"] for r in rows["rows"]]

    def test_history_day_range_includes_the_whole_last_day(self):
        self.assertEqual(
            self._history("2026-01-01", "2026-01-01"),
            ["2026-01-01 00:00:00", "2026-01-01 23:59:59"],
        )
        self.assertEqual(self._history("2024-02-29", "2024-02-29"), ["2024-02-29 12:00:00"])
        self.assertEqual(self._history(date_to="2025-12-31")[-1], "2025-12-31 23:59:59")
        self.assertEqual(self._history(date_from="2026-01-02"), ["2026-01-02 00:00:00"])

    def test_year_filters_stop_exactly_at_new_year(self):
        def plays(data):
            return sum(data["series"][0]["data"]) if data["series"] else 0

        self.assertEqual(plays(graphs.plays_over_time(year=2025, group="month")), 1)
        self.assertEqual(plays(graphs.plays_over_time(year=2026, group="month")), 3)
        summary = clients.build(
            days=None, year=2026, hide_unknown=True, watts=50, electricity_price=0.27
        )["summary"]
        self.assertEqual(summary["plays"], 3)
        self.assertEqual(wrapped.build("u1", 2026)["totals"]["plays"], 3)
        self.assertEqual(wrapped.build("u1", 2025)["totals"]["plays"], 1)

    def test_history_date_range_uses_the_started_at_index(self):
        captured = []
        real_query_one = database.query_one

        def spy(sql, params=()):
            captured.append((sql, params))
            return real_query_one(sql, params)

        with patch("jellyfin_stats.history.database.query_one", side_effect=spy):
            history.get_history(date_from="2026-01-01", date_to="2026-01-01")
        count_sql, params = captured[0]  # COUNT(*) filtré par la période

        plan = database.query("EXPLAIN QUERY PLAN " + count_sql, params)
        self.assertTrue(
            any("SEARCH" in r["detail"] and "started_at" in r["detail"] for r in plan), plan
        )


if __name__ == "__main__":
    unittest.main()
