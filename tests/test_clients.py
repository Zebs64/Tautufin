import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from jellyfin_stats import auth, clients, database
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app


class ClientStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tautufin.db")
        database.init(self.db_path)
        for user_id, name, hidden in (
            ("u1", "Alice", 0),
            ("u2", "Bob", 0),
            ("hidden", "Masqué", 1),
        ):
            database.execute(
                "INSERT INTO users (jellyfin_user_id, username, hidden) VALUES (?, ?, ?)",
                (user_id, name, hidden),
            )
        self._insert("u1", "Alpha", "DirectPlay", 3600, "1080p", "h264", "aac")
        self._insert("u1", "Alpha", "Transcode", 1800, "2160p", "hevc", "ac3")
        self._insert("u1", None, None, 0, None, None, None)
        self._insert("u2", "Beta", "DirectStream", 600, "720p", "h264", "aac")
        self._insert("hidden", "Secret", "Transcode", 7200, "4K", "hevc", "dts")

    def tearDown(self):
        self.tmp.cleanup()

    def _insert(self, user_id, client, method, duration, resolution, video, audio,
                started="datetime('now', 'localtime')"):
        database.execute(
            f"""
            INSERT INTO session_history
              (jellyfin_user_id, started_at, client_name, device_name, play_method,
               play_duration, video_resolution, video_codec, audio_codec)
            VALUES (?, {started}, ?, 'Device', ?, ?, ?, ?, ?)
            """,
            (user_id, client, method, duration, resolution, video, audio),
        )

    def test_global_summary_excludes_hidden_users_but_keeps_unknown_plays(self):
        result = clients.build(days=None, hide_unknown=True,
                               watts=100, electricity_price=0.27)

        self.assertEqual(result["summary"], {
            "active_clients": 2,
            "plays": 4,
            "duration_seconds": 6000,
            "direct_play_percent": 25.0,
            "direct_stream_percent": 25.0,
            "transcode_percent": 25.0,
            "transcode_seconds": 1800,
            "transcode_kwh": 0.05,
            "transcode_cost": 0.01,
        })
        self.assertEqual([row["client"] for row in result["clients"]], ["Alpha", "Beta"])
        self.assertEqual(result["clients"][0]["plays"], 2)
        self.assertEqual(result["clients"][0]["direct_play"], 1)
        self.assertEqual(result["clients"][0]["transcode"], 1)
        self.assertEqual(
            result["charts"]["video_codecs"]["categories"],
            ["h264", "Inconnu", "hevc"],
        )

    def test_user_scope_and_unknown_client_preferences_are_respected(self):
        hidden = clients.build(days=None, user_id="u1", hide_unknown=True)
        shown = clients.build(days=None, user_id="u1", hide_unknown=False,
                              unknown_label="Plex")

        self.assertEqual(hidden["summary"]["plays"], 3)
        self.assertEqual(hidden["summary"]["active_clients"], 1)
        self.assertEqual(shown["summary"]["active_clients"], 2)
        self.assertEqual([row["client"] for row in shown["clients"]], ["Alpha", "Plex"])
        self.assertEqual(shown["methods"]["unknown"], 1)

    def test_empty_result_has_zero_rates_cost_and_neutral_diagnostic(self):
        result = clients.build(days=None, user_id="missing", watts=100,
                               electricity_price=0.27)

        self.assertEqual(result["summary"]["plays"], 0)
        self.assertEqual(result["summary"]["direct_play_percent"], 0.0)
        self.assertEqual(result["summary"]["transcode_percent"], 0.0)
        self.assertEqual(result["summary"]["transcode_cost"], 0.0)
        self.assertEqual(result["diagnostics"], [])
        self.assertEqual(result["clients"], [])

    def test_active_client_kpi_is_not_limited_by_bounded_table_and_charts(self):
        for index in range(11):
            self._insert("many", f"Client {index:02d}", "DirectPlay", 60,
                         "1080p", "h264", "aac")

        result = clients.build(days=None, user_id="many")

        self.assertEqual(result["summary"]["active_clients"], 11)
        self.assertEqual(len(result["clients"]), 10)

    def test_year_filter_and_available_years_use_the_same_scope(self):
        old_year = date.today().year - 1
        self._insert("u1", "Old", "DirectPlay", 60, "480p", "h264", "aac",
                     started=f"'{old_year}-03-01 12:00:00'")

        current = clients.build(days=None, user_id="u1", year=date.today().year)
        old = clients.build(days=None, user_id="u1", year=old_year)

        self.assertEqual(current["summary"]["plays"], 3)
        self.assertEqual(old["summary"]["plays"], 1)
        self.assertEqual(clients.available_years("u1"), [date.today().year, old_year])


class ClientRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = Config(str(root / "config.ini"))
        self.config.set("Database", "path", str(root / "tautufin.db"))
        self.config.save()
        database.init(self.config.database_path)
        database.execute(
            "INSERT INTO users (jellyfin_user_id, username) VALUES ('u1', 'Alice')"
        )
        database.execute(
            """
            INSERT INTO session_history
              (jellyfin_user_id, started_at, client_name, play_method, play_duration)
            VALUES ('u1', datetime('now', 'localtime'), 'Alpha', 'DirectPlay', 60)
            """
        )
        self.app = create_app(self.config)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_standard_user_page_and_api_are_scoped_from_the_session(self):
        user = auth.CurrentUser("token", "local", "Alice", "user", "u1")
        with patch("jellyfin_stats.auth.resolve_session", return_value=user):
            page = self.client.get("/clients")
            payload = self.client.get("/api/clients?days=0&user_id=someone-else")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Diagnostic clients", page.text)
        self.assertNotIn('id="clients-user"', page.text)
        self.assertEqual(payload.status_code, 200)
        self.assertEqual(payload.json()["summary"]["plays"], 1)

    def test_vision_user_gets_global_page_filter(self):
        user = auth.CurrentUser("token", "local", "Vision", "user", "u1", can_view_all=True)
        with patch("jellyfin_stats.auth.resolve_session", return_value=user):
            page = self.client.get("/clients")

        self.assertEqual(page.status_code, 200)
        self.assertIn('id="clients-user"', page.text)
        self.assertIn('href="/clients" class="active"', page.text)


if __name__ == "__main__":
    unittest.main()
