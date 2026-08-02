import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from jellyfin_stats import __version__, auth, database
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app


class ReleaseV021Tests(unittest.TestCase):
    def test_application_reports_version_0_2_1(self):
        self.assertEqual(__version__, "0.2.1")

    def test_schema_is_v6(self):
        with tempfile.TemporaryDirectory() as tmp:
            database.init(str(Path(tmp) / "tautufin.db"))
            version = database.query_one("SELECT version FROM schema_version")
        self.assertEqual(version["version"], 6)

    def test_clients_route_navigation_and_static_assets_are_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config(str(root / "config.ini"))
            config.set("Database", "path", str(root / "tautufin.db"))
            config.save()
            database.init(config.database_path)
            app = create_app(config)
            client = TestClient(app)
            user = auth.CurrentUser("token", "local", "Admin", "admin", None)
            with patch("jellyfin_stats.auth.resolve_session", return_value=user):
                page = client.get("/clients")
                script = client.get("/static/js/clients.js")

        self.assertEqual(page.status_code, 200)
        self.assertIn('href="/clients"', page.text)
        self.assertIn("chart-state.js", page.text)
        self.assertLess(page.text.index("chart-state.js"), page.text.index("clients.js"))
        self.assertEqual(script.status_code, 200)
        self.assertIn("ChartState.load", script.text)


if __name__ == "__main__":
    unittest.main()
