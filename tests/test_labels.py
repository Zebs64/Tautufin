import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from jellyfin_stats import auth, database, graphs
from jellyfin_stats.config import Config
from jellyfin_stats.labels import (
    collection_label,
    date_long_label,
    datetime_label,
    media_type_label,
    play_method_label,
)
from jellyfin_stats.main import create_app
from jellyfin_stats.web.helpers import _datetime_html


class LabelFunctionsTests(unittest.TestCase):
    def test_media_and_collection_types_are_translated(self):
        self.assertEqual(media_type_label("Episode"), "Épisode")
        self.assertEqual(media_type_label("Movie"), "Film")
        self.assertEqual(media_type_label(None), "Inconnu")
        self.assertEqual(media_type_label("NouveauType"), "NouveauType")
        self.assertEqual(collection_label("tvshows"), "Séries")
        self.assertEqual(collection_label(None), "Bibliothèque")

    def test_play_methods_including_transcode_details(self):
        self.assertEqual(play_method_label("DirectPlay"), "Lecture directe")
        self.assertEqual(play_method_label("Transcode"), "Transcodage")
        self.assertEqual(
            play_method_label("Transcode (v:h264 a:aac)"),
            "Transcodage (vidéo H264, audio AAC)",
        )
        self.assertEqual(
            play_method_label("Transcode (v:direct a:direct)"),
            "Transcodage (vidéo directe, audio direct)",
        )
        self.assertEqual(play_method_label(""), "Inconnu")


class DateLabelsTests(unittest.TestCase):
    TODAY = date(2026, 11, 11)

    def test_relative_then_short_dates(self):
        cases = {
            "2026-11-11 22:31:05": "aujourd'hui 22:31",
            "2026-11-10 08:02:00": "hier 08:02",
            "2026-08-01 09:05:00": "1er août 09:05",
            "2025-11-10 22:31:00": "10 nov. 2025 22:31",
        }
        for value, expected in cases.items():
            self.assertEqual(datetime_label(value, today=self.TODAY), expected)
        self.assertEqual(datetime_label("pas une date", today=self.TODAY), "pas une date")

    def test_long_dates(self):
        self.assertEqual(date_long_label("2025-11-10"), "10 novembre 2025")
        self.assertEqual(date_long_label("2018-08-01 20:32:00"), "1er août 2018")

    def test_template_filter_keeps_the_exact_date_in_a_tooltip_and_escapes(self):
        html = str(_datetime_html("2025-11-10 22:31:00"))
        self.assertIn('title="2025-11-10 22:31"', html)
        self.assertIn(">10 nov. 2025 22:31</time>", html)
        self.assertEqual(_datetime_html(None), "")
        self.assertNotIn("<script>", str(_datetime_html("<script>x</script>")))


class LabelsInPagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = Config(str(root / "config.ini"))
        config.set("Database", "path", str(root / "tautufin.db"))
        config.save()
        database.init(config.database_path)
        database.execute("INSERT INTO users (jellyfin_user_id, username) VALUES ('u1', 'Alice')")
        for item_type, method in (("Episode", "DirectPlay"), ("Movie", ""), ("Movie", None)):
            database.execute(
                "INSERT INTO session_history (jellyfin_user_id, item_id, item_type, item_name,"
                " started_at, play_duration, play_method)"
                " VALUES ('u1', ?, ?, 'Titre', datetime('now', 'localtime', '-1 day'), 600, ?)",
                (f"i-{item_type}-{method}", item_type, method),
            )
        self.client = TestClient(create_app(config))
        admin = auth.CurrentUser("t", "local", "admin", "admin", None)
        session = patch("jellyfin_stats.auth.resolve_session", return_value=admin)
        session.start()
        self.addCleanup(session.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_play_methods_form_a_single_unknown_slice(self):
        data = graphs.by_play_method(days=30)

        self.assertEqual(sorted(data["categories"]), ["Inconnu", "Lecture directe"])
        unknown = data["categories"].index("Inconnu")
        self.assertEqual(data["series"][0]["data"][unknown], 2)

    def test_history_api_and_pages_show_french_types(self):
        rows = self.client.get("/api/history").json()["rows"]
        history_page = self.client.get("/history").text
        profile = self.client.get("/users/u1").text

        self.assertEqual({r["item_type_label"] for r in rows}, {"Épisode", "Film"})
        self.assertEqual({r["item_type"] for r in rows}, {"Episode", "Movie"})  # clés brutes
        self.assertIn('<option value="Movie">Film</option>', history_page)
        self.assertIn("Épisode</span>", profile)


if __name__ == "__main__":
    unittest.main()
