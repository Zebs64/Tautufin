import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bcrypt
from fastapi.testclient import TestClient

from jellyfin_stats import auth, database
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app, reset_password_cli

LONG = "x" * 73


class PasswordLengthTests(unittest.TestCase):
    def test_72_bytes_is_accepted_and_73_bytes_is_refused(self):
        self.assertTrue(auth.check_password("x" * 72, auth.hash_password("x" * 72)))
        with self.assertRaises(auth.PasswordTooLong):
            auth.hash_password(LONG)

    def test_limit_counts_bytes_not_characters(self):
        self.assertIsNone(auth.password_error("é" * 36))  # 72 octets
        self.assertIsNotNone(auth.password_error("é" * 37))  # 74 octets

    def test_hash_made_by_truncating_bcrypt_still_verifies_long_password(self):
        legacy_hash = bcrypt.hashpw(LONG.encode()[:72], bcrypt.gensalt()).decode()

        self.assertTrue(auth.check_password(LONG, legacy_hash))
        self.assertFalse(auth.check_password("y" * 73, legacy_hash))


class PasswordLengthRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = Config(str(root / "config.ini"))
        self.config.set("Database", "path", str(root / "tautufin.db"))
        self.config.save()
        database.init(self.config.database_path)
        self.client = TestClient(create_app(self.config))

    def tearDown(self):
        self.tmp.cleanup()

    def _as_admin(self):
        user = auth.CurrentUser("token", "local", "admin", "admin", None)
        return patch("jellyfin_stats.auth.resolve_session", return_value=user)

    def test_setup_refuses_too_long_admin_password(self):
        response = self.client.post(
            "/setup", data={"admin_username": "admin", "admin_password": LONG}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("72 octets maximum", response.text)
        self.assertIsNone(auth.get_local_user("admin"))

    def test_settings_refuse_too_long_password_on_create_and_reset(self):
        auth.create_local_user("admin", "secret", "admin")
        admin_id = auth.get_local_user("admin")["id"]
        with self._as_admin():
            created = self.client.post(
                "/settings/local-users", data={"username": "bob", "password": LONG}
            )
            reset = self.client.post(
                f"/settings/local-users/{admin_id}/password", data={"new_password": LONG}
            )

        for response in (created, reset):
            self.assertEqual(response.status_code, 200)
            self.assertIn("72 octets maximum", response.text)
        self.assertIsNone(auth.get_local_user("bob"))
        self.assertTrue(auth.login_local("admin", "secret"))

    def test_cli_reset_refuses_too_long_password(self):
        auth.create_local_user("admin", "secret", "admin")
        with patch("jellyfin_stats.main.getpass.getpass", return_value=LONG):
            self.assertEqual(reset_password_cli("admin"), 1)
        self.assertTrue(
            auth.check_password("secret", auth.get_local_user("admin")["password_hash"])
        )


if __name__ == "__main__":
    unittest.main()
