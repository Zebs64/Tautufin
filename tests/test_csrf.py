import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from jellyfin_stats import auth, database
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app

LOGIN = {"mode": "local", "username": "admin", "password": "secret"}


class OriginCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = Config(str(root / "config.ini"))
        config.set("Database", "path", str(root / "tautufin.db"))
        config.save()
        database.init(config.database_path)
        auth.create_local_user("admin", "secret", "admin")
        self.client = TestClient(create_app(config), base_url="http://tautufin.local")

    def tearDown(self):
        self.tmp.cleanup()

    def _login(self, **headers):
        return self.client.post("/login", data=LOGIN, headers=headers, follow_redirects=False)

    def test_cross_origin_post_is_rejected(self):
        for headers in (
            {"Origin": "http://evil.example"},
            {"Origin": "http://autre.tautufin.local"},  # sous-domaine du même site
            {"Origin": "null"},  # iframe sandboxée, file://
            {"Referer": "https://evil.example/page"},
        ):
            with self.assertLogs("jellyfin_stats.main", level="WARNING"):
                self.assertEqual(self._login(**headers).status_code, 403, headers)

    def test_same_origin_and_non_browser_posts_pass(self):
        self.assertEqual(self._login(Origin="http://tautufin.local").status_code, 303)
        self.assertEqual(self._login(Referer="http://tautufin.local/login").status_code, 303)
        self.assertEqual(self._login().status_code, 303)  # ni Origin ni Referer

    def test_forwarded_host_from_a_proxy_rewriting_host_is_accepted(self):
        response = self._login(
            Origin="https://stats.example",
            Host="tautufin:8181",
            **{"X-Forwarded-Host": "stats.example"},
        )
        self.assertEqual(response.status_code, 303)

    def test_reads_are_not_checked(self):
        response = self.client.get("/login", headers={"Referer": "https://evil.example/"})
        self.assertEqual(response.status_code, 200)

    def test_logout_requires_post_and_closes_the_session(self):
        cookie = self._login().cookies.get(auth.SESSION_COOKIE)
        self.client.cookies.set(auth.SESSION_COOKIE, cookie)

        self.assertEqual(self.client.get("/logout").status_code, 405)
        self.assertIsNotNone(auth.resolve_session(cookie))

        response = self.client.post(
            "/logout", headers={"Origin": "http://tautufin.local"}, follow_redirects=False
        )
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(auth.resolve_session(cookie))


if __name__ == "__main__":
    unittest.main()
