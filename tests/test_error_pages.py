import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from jellyfin_stats import auth, database
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app


class ErrorPagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = Config(str(root / "config.ini"))
        config.set("Database", "path", str(root / "tautufin.db"))
        config.save()
        database.init(config.database_path)
        self.client = TestClient(create_app(config))

    def tearDown(self):
        self.tmp.cleanup()

    def test_regular_user_gets_an_explicit_html_403_on_admin_pages(self):
        bob = auth.CurrentUser("t", "jellyfin", "bob", "user", "u1")
        with patch("jellyfin_stats.auth.resolve_session", return_value=bob):
            response = self.client.get("/settings")

        self.assertEqual(response.status_code, 403)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Réservé aux administrateurs", response.text)
        self.assertIn("bob", response.text)  # barre de navigation de l'utilisateur

    def test_anonymous_page_redirects_to_login_but_api_answers_json(self):
        page = self.client.get("/history", follow_redirects=False)
        api = self.client.get("/api/history")

        self.assertEqual((page.status_code, page.headers["location"]), (303, "/login"))
        self.assertEqual(api.status_code, 401)
        self.assertEqual(api.json(), {"detail": "Authentification requise"})


if __name__ == "__main__":
    unittest.main()
