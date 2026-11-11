import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from jellyfin_stats import auth, database
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app, main


class ReverseProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = Config(str(self.root / "config.ini"))
        self.config.set("Database", "path", str(self.root / "tautufin.db"))
        self.config.save()
        database.init(self.config.database_path)
        auth.create_local_user("admin", "secret", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _login_cookie_header(self, base_url):
        client = TestClient(create_app(self.config), base_url=base_url)
        response = client.post(
            "/login",
            data={"mode": "local", "username": "admin", "password": "secret"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        return response.headers["set-cookie"]

    def test_session_cookie_is_secure_over_https_only(self):
        self.assertIn("secure", self._login_cookie_header("https://tautufin.example").lower())
        self.assertNotIn("secure", self._login_cookie_header("http://tautufin.example").lower())

    def test_trusted_proxies_are_passed_to_uvicorn(self):
        self.config.set("Web", "trusted_proxies", "172.18.0.0/16, 10.0.0.2")
        self.config.save()

        with patch("jellyfin_stats.main.uvicorn.run") as run:
            main(["--config", self.config.path])

        kwargs = run.call_args.kwargs
        self.assertTrue(kwargs["proxy_headers"])
        self.assertEqual(kwargs["forwarded_allow_ips"], "172.18.0.0/16, 10.0.0.2")


if __name__ == "__main__":
    unittest.main()
