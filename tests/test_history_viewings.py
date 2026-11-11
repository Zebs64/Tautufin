import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from jellyfin_stats import auth, database
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app

ADMIN = auth.CurrentUser("t", "local", "admin", "admin", None)
BOB = auth.CurrentUser("t", "jellyfin", "bob", "user", "u2")
FILM, DOC = "f" * 32, "d" * 32  # identifiants au format Jellyfin


class GroupedHistoryTests(unittest.TestCase):
    """L'historique affiche une ligne par visionnage ; l'export reste brut."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = Config(str(root / "config.ini"))
        config.set("Database", "path", str(root / "tautufin.db"))
        config.save()
        database.init(config.database_path)
        rows = [
            # u1 : film vu en trois fois (le dernier segment sur un autre client).
            ("u1", FILM, "live", "2026-11-01 20:00:00", "2026-11-01 20:30:00", 1800, 30.0, "TV"),
            ("u1", FILM, "live", "2026-11-01 21:00:00", "2026-11-01 21:50:00", 3000, 80.0, "TV"),
            ("u1", FILM, "live", "2026-11-02 18:00:00", "2026-11-02 18:20:00", 1200, 99.0, "Web"),
            # u1 : film importé en deux parties (progression = part cumulée).
            ("u1", DOC, "import", "2026-11-04 20:00:00", None, 2400, 40.0, "Web"),
            ("u1", DOC, "import", "2026-11-05 20:00:00", None, 4200, 70.0, "Web"),
            # u2 : visionnage d'une traite.
            ("u2", FILM, "live", "2026-11-03 20:00:00", "2026-11-03 21:40:00", 6000, 98.0, "TV"),
        ]
        for user_id, item, source, start, stop, duration, percent, client in rows:
            database.execute(
                """
                INSERT INTO session_history (source, jellyfin_user_id, user_name, item_id,
                    item_type, item_name, started_at, stopped_at, play_duration,
                    runtime_seconds, percent_complete, client_name, ip_address)
                VALUES (?, ?, ?, ?, 'Movie', ?, ?, ?, ?, 6000, ?, ?, '192.0.2.1')
                """,
                (
                    source,
                    user_id,
                    user_id.upper(),
                    item,
                    item,
                    start,
                    stop,
                    duration,
                    percent,
                    client,
                ),
            )
        database.init(config.database_path)
        self.db_path = config.database_path
        self.client = TestClient(create_app(config))

    def tearDown(self):
        self.tmp.cleanup()

    def _get(self, user, path):
        with patch("jellyfin_stats.auth.resolve_session", return_value=user):
            return self.client.get(path)

    def test_one_row_per_viewing_with_totals_and_final_progress(self):
        data = self._get(ADMIN, "/api/history?sort=date&order=asc").json()

        self.assertEqual(data["total"], 3)
        rows = {(r["jellyfin_user_id"], r["item_id"]): r for r in data["rows"]}
        film = rows[("u1", FILM)]
        self.assertEqual(
            (film["started_at"], film["segments"], film["play_duration"]),
            ("2026-11-01 20:00:00", 3, 6000),
        )
        self.assertEqual(film["percent_complete"], 99.0)  # position finale
        self.assertEqual(film["client_name"], "TV")  # client du premier segment
        self.assertEqual(film["stopped_at"], "2026-11-02 18:20:00")
        doc = rows[("u1", DOC)]
        self.assertEqual((doc["segments"], doc["percent_complete"]), (2, 100.0))
        self.assertEqual(rows[("u2", FILM)]["segments"], 1)

    def test_sorting_by_total_duration_and_date_filter_on_first_segment(self):
        by_duration = self._get(ADMIN, "/api/history?sort=duration&order=desc").json()["rows"]
        later = self._get(ADMIN, "/api/history?date_from=2026-11-02").json()

        self.assertEqual([r["play_duration"] for r in by_duration], [6600, 6000, 6000])
        self.assertEqual(later["total"], 2)  # le film de u1 date du 1er novembre

    def test_segments_endpoint_respects_isolation(self):
        film_id = next(
            r["id"]
            for r in self._get(ADMIN, "/api/history").json()["rows"]
            if r["jellyfin_user_id"] == "u1" and r["item_id"] == FILM
        )

        segments = self._get(ADMIN, f"/api/history/viewings/{film_id}/segments").json()
        forbidden = self._get(BOB, f"/api/history/viewings/{film_id}/segments")

        self.assertEqual([s["play_duration"] for s in segments["segments"]], [1800, 3000, 1200])
        self.assertEqual(segments["segments"][2]["client_name"], "Web")
        self.assertEqual(forbidden.status_code, 404)

    def test_regular_user_sees_own_viewings_without_ip(self):
        rows = self._get(BOB, "/api/history?user_id=u1").json()["rows"]

        self.assertEqual([(r["jellyfin_user_id"], r["segments"]) for r in rows], [("u2", 1)])
        self.assertNotIn("ip_address", rows[0])

    def test_recent_lists_on_profile_and_media_pages_are_grouped(self):
        profile = self._get(ADMIN, "/users/u1").text
        film = self._get(ADMIN, f"/media/{FILM}").text

        self.assertEqual(profile.count("en 3 fois"), 1)
        self.assertEqual(profile.count("en 2 fois"), 1)
        self.assertEqual(film.count("en 3 fois"), 1)
        recent = film.split("récentes</h2>")[1]
        self.assertEqual(recent.count('href="/users/'), 2)  # une ligne par visionnage

    def test_estimated_plays_are_flagged_with_their_origin(self):
        for source, start, stop in (
            ("infer", "2026-10-01 20:00:00", None),
            ("import", "2026-10-02 20:00:00", "2026-10-02 20:00:00"),  # Streamystats
        ):
            database.execute(
                "INSERT INTO session_history (source, jellyfin_user_id, item_id, item_type,"
                " item_name, started_at, stopped_at, play_duration)"
                " VALUES (?, 'u2', ?, 'Movie', 'Ancien', ?, ?, 6000)",
                (source, "e" * 31 + source[0], start, stop),
            )
        database.init(self.db_path)

        rows = self._get(ADMIN, "/api/history?user_id=u2&sort=date&order=asc").json()["rows"]

        self.assertEqual([r["estimated"] for r in rows], [True, True, False])
        self.assertIn("statut « Lu »", rows[0]["estimate_hint"])
        self.assertIn("Streamystats", rows[1]["estimate_hint"])
        self.assertIsNone(rows[2]["estimate_hint"])
        self.assertIn("estimée</span>", self._get(ADMIN, "/users/u2").text)

    def test_csv_export_stays_per_segment(self):
        csv_text = self._get(ADMIN, "/api/history.csv").text

        self.assertEqual(len(csv_text.strip().splitlines()), 1 + 6)


if __name__ == "__main__":
    unittest.main()
