import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from jellyfin_stats import auth, database
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app, main
from jellyfin_stats.proxy import normalize_trusted_proxies

LOGIN = {"mode": "local", "username": "admin", "password": "secret"}


class NormalizeTrustedProxiesTests(unittest.TestCase):
    def test_valid_entries_are_normalized(self):
        self.assertEqual(normalize_trusted_proxies(""), "127.0.0.1")
        self.assertEqual(normalize_trusted_proxies(" * "), "*")
        self.assertEqual(
            normalize_trusted_proxies("172.18.0.1/16; 10.0.0.2, 10.0.0.2, ::1"),
            "172.18.0.0/16, 10.0.0.2, ::1",
        )

    def test_invalid_entries_are_refused_with_a_readable_message(self):
        for value, fragment in (
            ("traefik", "« traefik »"),
            ("172.18.0.0/40", "« 172.18.0.0/40 »"),
            ("*, 10.0.0.1", "ne se combine pas"),
        ):
            with self.assertRaisesRegex(ValueError, fragment):
                normalize_trusted_proxies(value)


class ReverseProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = Config(str(self.root / "config.ini"))
        self.config.set("Database", "path", str(self.root / "tautufin.db"))
        self.config.save()
        database.init(self.config.database_path)
        auth.create_local_user("admin", "secret", "admin")
        self.client = TestClient(create_app(self.config), base_url="http://tautufin.local")

    def tearDown(self):
        self.tmp.cleanup()

    def _login(self, password="secret", **headers):
        return self.client.post(
            "/login",
            data={**LOGIN, "password": password},
            headers=headers,
            follow_redirects=False,
        )

    def _save_trusted(self, value):
        self.client.cookies.set(auth.SESSION_COOKIE, self._login().cookies[auth.SESSION_COOKIE])
        response = self.client.post(
            "/settings/access",
            data={"session_lifetime": 604800, "local_auth_enabled": "on", "trusted_proxies": value},
            follow_redirects=False,
        )
        self.client.cookies.clear()
        return response

    def test_session_cookie_is_secure_over_https_only(self):
        https = TestClient(create_app(self.config), base_url="https://tautufin.local")
        secure = https.post("/login", data=LOGIN, follow_redirects=False).headers["set-cookie"]
        self.assertIn("secure", secure.lower())
        self.assertNotIn("secure", self._login().headers["set-cookie"].lower())

    def test_trust_set_in_settings_applies_without_restart(self):
        forwarded_https = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.7"}
        # Client de test non listé : en-têtes ignorés, cookie non sécurisé.
        self.assertNotIn("secure", self._login(**forwarded_https).headers["set-cookie"].lower())

        self.assertEqual(self._save_trusted("*").status_code, 303)

        self.assertEqual(self.config.get("Web", "trusted_proxies"), "*")
        self.assertIn("secure", self._login(**forwarded_https).headers["set-cookie"].lower())

    def test_login_rate_limit_counts_real_client_ips_behind_a_trusted_proxy(self):
        def failed_logins(client):
            return [
                client.post(
                    "/login",
                    data={**LOGIN, "password": "faux"},
                    headers={"X-Forwarded-For": f"203.0.113.{index}"},
                ).status_code
                for index in range(6)
            ]

        # Proxy non listé : tout le monde partage son IP -> 6e tentative bloquée.
        untrusted = TestClient(create_app(self.config), base_url="http://tautufin.local")
        self.assertEqual(failed_logins(untrusted)[-1], 429)

        self._save_trusted("*")

        self.assertNotIn(429, failed_logins(self.client))

    def test_invalid_value_in_settings_is_refused_and_not_saved(self):
        response = self._save_trusted("traefik")

        self.assertEqual(response.status_code, 200)
        self.assertIn("« traefik »", response.text)
        self.assertEqual(self.config.trusted_proxies, "127.0.0.1")

    def test_invalid_hand_edited_value_falls_back_to_default(self):
        self.config.set("Web", "trusted_proxies", "traefik")

        with self.assertLogs("jellyfin_stats.config", level="WARNING") as logs:
            for _ in range(3):  # lue à chaque requête
                self.assertEqual(self.config.trusted_proxies, "127.0.0.1")
        self.assertEqual(len(logs.records), 1)

    def test_uvicorn_does_not_process_proxy_headers_twice(self):
        with patch("jellyfin_stats.main.uvicorn.run") as run:
            main(["--config", self.config.path])

        self.assertFalse(run.call_args.kwargs["proxy_headers"])


if __name__ == "__main__":
    unittest.main()
