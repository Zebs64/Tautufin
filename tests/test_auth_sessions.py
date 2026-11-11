import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from jellyfin_stats import auth, database
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app


class SessionRevocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        database.init(str(Path(self.tmp.name) / "tautufin.db"))
        auth.init("test-secret", 3600)

    def tearDown(self):
        self.tmp.cleanup()

    def _login_local(self, username, password="secret", role="user", jellyfin_user_id=None):
        if not auth.get_local_user(username):
            auth.create_local_user(username, password, role, jellyfin_user_id)
        return auth.cookie_value(auth.login_local(username, password))

    def _local_id(self, username):
        return auth.get_local_user(username)["id"]

    def test_deleting_local_user_closes_its_sessions(self):
        auth.create_local_user("admin", "secret", "admin")
        cookie = self._login_local("bob")

        auth.delete_local_user(self._local_id("bob"))

        self.assertIsNone(auth.resolve_session(cookie))
        self.assertIsNone(database.query_one("SELECT 1 FROM http_sessions WHERE username = 'bob'"))

    def test_password_change_closes_all_sessions_of_the_account_only(self):
        first = self._login_local("bob")
        second = self._login_local("bob")
        other = self._login_local("alice")

        self.assertTrue(auth.set_local_password("bob", "nouveau"))

        self.assertIsNone(auth.resolve_session(first))
        self.assertIsNone(auth.resolve_session(second))
        self.assertIsNotNone(auth.resolve_session(other))
        self.assertIsNotNone(auth.login_local("bob", "nouveau"))

    def test_last_local_admin_cannot_be_deleted(self):
        auth.create_local_user("admin", "secret", "admin")

        with self.assertRaises(auth.LastAdminError):
            auth.delete_local_user(self._local_id("admin"))

        auth.create_local_user("admin2", "secret", "admin")
        auth.delete_local_user(self._local_id("admin"))
        self.assertIsNone(auth.get_local_user("admin"))

    def test_local_role_and_link_are_read_on_each_request(self):
        cookie = self._login_local("bob", role="admin")
        self.assertTrue(auth.resolve_session(cookie).is_admin)

        database.execute(
            "UPDATE local_users SET role = 'user', jellyfin_user_id = 'u1' WHERE username = 'bob'"
        )

        user = auth.resolve_session(cookie)
        self.assertFalse(user.is_admin)
        self.assertEqual(user.jellyfin_user_id, "u1")

    def test_demoted_jellyfin_admin_loses_admin_role_without_relogin(self):
        database.execute(
            "INSERT INTO users (jellyfin_user_id, username, is_admin) VALUES ('u1', 'Alice', 1)"
        )
        session = auth._create_session("jellyfin", "Alice", "admin", "u1")
        cookie = auth.cookie_value(session)
        self.assertTrue(auth.resolve_session(cookie).is_admin)

        database.execute("UPDATE users SET is_admin = 0 WHERE jellyfin_user_id = 'u1'")

        self.assertFalse(auth.resolve_session(cookie).is_admin)


class LocalUserRouteTests(unittest.TestCase):
    def test_deleting_last_admin_from_settings_shows_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config(str(root / "config.ini"))
            config.set("Database", "path", str(root / "tautufin.db"))
            config.save()
            database.init(config.database_path)
            auth.create_local_user("admin", "secret", "admin")
            admin_id = auth.get_local_user("admin")["id"]
            client = TestClient(create_app(config))
            user = auth.CurrentUser("token", "local", "admin", "admin", None)
            with patch("jellyfin_stats.auth.resolve_session", return_value=user):
                response = client.post(f"/settings/local-users/{admin_id}/delete")

            self.assertEqual(response.status_code, 200)
            self.assertIn("dernier compte admin local", response.text)
            self.assertIsNotNone(auth.get_local_user("admin"))


if __name__ == "__main__":
    unittest.main()
