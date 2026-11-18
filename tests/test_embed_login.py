"""Connexion automatique depuis un onglet personnalisé de Jellyfin : la page
de connexion transmet le jeton de la session Jellyfin Web parente, que le
serveur fait vérifier par Jellyfin (GET /Users/Me)."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from jellyfin_stats import auth, database
from jellyfin_stats.config import Config, normalize_origins
from jellyfin_stats.jellyfin_api import JellyfinAPI
from jellyfin_stats.main import create_app

TOKEN = "0123456789abcdef0123456789abcdef"
ALICE = {"Id": "u-alice", "Name": "Alice", "Policy": {"IsAdministrator": False}}
ROOT = {"Id": "u-root", "Name": "Root", "Policy": {"IsAdministrator": True}}
ORIGIN = "http://tautufin.local"


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}" if payload is not None else b""

    def json(self):
        return self._payload


class EmbedLoginRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = Config(str(root / "config.ini"))
        self.config.set("Database", "path", str(root / "tautufin.db"))
        self.config.set("Jellyfin", "url", "http://jellyfin.internal:8096")
        self.config.set("Jellyfin", "api_key", "api-key")
        self.config.set("Auth", "jellyfin_web_origins", "https://jellyfin.example")
        self.config.save()
        database.init(self.config.database_path)
        auth.create_local_user("admin", "secret", "admin")
        self.client = TestClient(create_app(self.config), base_url=ORIGIN)
        self.users = {TOKEN: ALICE}
        self.calls = []

        def fake_current_user(api, token):
            self.calls.append(token)
            return self.users.get(token)

        patcher = patch.object(JellyfinAPI, "get_current_user", fake_current_user)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _post(self, token=TOKEN, **headers):
        headers.setdefault("Origin", ORIGIN)
        return self.client.post("/login/jellyfin-session", json={"token": token}, headers=headers)

    def test_valid_token_opens_a_session_for_the_jellyfin_user(self):
        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls, [TOKEN])
        user = auth.resolve_session(response.cookies.get(auth.SESSION_COOKIE))
        self.assertEqual(
            (user.auth_mode, user.username, user.role, user.jellyfin_user_id),
            ("jellyfin", "Alice", "user", "u-alice"),
        )

    def test_admin_role_comes_from_jellyfin(self):
        self.users = {TOKEN: ROOT}
        user = auth.resolve_session(self._post().cookies.get(auth.SESSION_COOKIE))
        self.assertTrue(user.is_admin)

    def test_jellyfin_token_is_never_stored(self):
        self.assertEqual(self._post().status_code, 200)
        with database.db() as conn:
            dump = "\n".join(conn.iterdump())
        self.assertIn("u-alice", dump)
        self.assertNotIn(TOKEN, dump)

    def test_token_refused_by_jellyfin_is_rejected(self):
        response = self._post(token="f" * 32)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Session Jellyfin non reconnue.")
        self.assertIsNone(response.cookies.get(auth.SESSION_COOKIE))

    def test_malformed_token_never_reaches_jellyfin(self):
        for token in ("", "short", 'abc"def0123456789abcdef', "a" * 200, "x y" * 10):
            self.assertEqual(self._post(token=token).status_code, 401, token)
        self.assertEqual(self.calls, [])

    def test_blocked_user_is_refused(self):
        self._post()
        database.execute("UPDATE users SET access_blocked = 1 WHERE jellyfin_user_id = 'u-alice'")
        with self.assertLogs("jellyfin_stats.web.auth_routes", level="WARNING"):
            response = self._post()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Votre accès à Tautufin a été désactivé.")

    def test_post_from_the_jellyfin_page_itself_is_rejected(self):
        with self.assertLogs("jellyfin_stats.main", level="WARNING"):
            response = self._post(Origin="https://jellyfin.example")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.calls, [])

    def test_disabled_without_origins_or_jellyfin_auth(self):
        for section, key, value in (
            ("Auth", "jellyfin_web_origins", ""),
            ("Auth", "jellyfin_web_origins", "pas une url"),
            ("Auth", "jellyfin_auth_enabled", "false"),
        ):
            with self.subTest(key=key, value=value):
                self.config.set("Auth", "jellyfin_web_origins", "https://jellyfin.example")
                self.config.set("Auth", "jellyfin_auth_enabled", "true")
                self.config.set(section, key, value)
                self.assertEqual(self._post().status_code, 404)
                self.assertNotIn('id="embed-login"', self.client.get("/login").text)
        self.assertEqual(self.calls, [])

    def test_login_page_lists_allowed_origins_only_when_enabled(self):
        page = self.client.get("/login").text
        self.assertIn('id="embed-login" data-origins="https://jellyfin.example"', page)
        self.assertIn('data-auto="true"', page)

    def test_failed_form_login_does_not_retry_automatically(self):
        # Après une erreur (ex. mauvais mot de passe d'un compte local), ne pas
        # reconnecter en silence avec la session Jellyfin.
        page = self.client.post(
            "/login",
            data={"mode": "local", "username": "admin", "password": "faux"},
            headers={"Origin": ORIGIN},
        ).text
        self.assertIn("Identifiants invalides", page)
        self.assertIn('id="embed-login"', page)
        self.assertIn('data-auto="false"', page)

    def test_invalid_origins_warn_once_per_value(self):
        self.config.set("Auth", "jellyfin_web_origins", "pas une url")
        with self.assertLogs("jellyfin_stats.config", level="WARNING") as logs:
            for _ in range(3):
                self.assertEqual(self.config.jellyfin_web_origins, [])
        self.assertEqual(len(logs.records), 1)

    def test_attempts_share_the_login_rate_limit(self):
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            self._post(token="f" * 32)
        with self.assertLogs("jellyfin_stats.web.auth_routes", level="WARNING"):
            self.assertEqual(self._post().status_code, 429)

    def test_logout_disables_automatic_login_on_the_next_page(self):
        self.client.cookies.set(auth.SESSION_COOKIE, self._post().cookies[auth.SESSION_COOKIE])
        response = self.client.post("/logout", headers={"Origin": ORIGIN}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/login?manual=1")

    def test_settings_validate_and_normalize_origins(self):
        login = {"mode": "local", "username": "admin", "password": "secret"}
        self.client.post("/login", data=login, headers={"Origin": ORIGIN})
        form = {"session_lifetime": "3600", "jellyfin_auth_enabled": "on"}

        bad = self.client.post(
            "/settings/access",
            data={**form, "jellyfin_web_origins": "https://jellyfin.example/web"},
            headers={"Origin": ORIGIN},
        )
        self.assertIn("Origine invalide", bad.text)
        self.assertEqual(self.config.jellyfin_web_origins, ["https://jellyfin.example"])

        self.client.post(
            "/settings/access",
            data={**form, "jellyfin_web_origins": "https://JF.example/ , http://10.0.0.2:8096"},
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(
            self.config.jellyfin_web_origins, ["https://jf.example", "http://10.0.0.2:8096"]
        )


class NormalizeOriginsTests(unittest.TestCase):
    def test_valid_origins_are_normalized_and_deduplicated(self):
        self.assertEqual(
            normalize_origins(" https://JF.example/, http://jf:8096,https://jf.example "),
            "https://jf.example, http://jf:8096",
        )
        self.assertEqual(normalize_origins(""), "")

    def test_anything_but_a_bare_origin_is_rejected(self):
        for value in (
            "jf.example",
            "ftp://jf.example",
            "https://jf.example/web",
            "https://jf.example?x=1",
            "https://user:pw@jf.example",
            "https://jf.example:99999",
            "https://",
        ):
            with self.assertRaises(ValueError, msg=value):
                normalize_origins(value)


class GetCurrentUserTests(unittest.TestCase):
    def setUp(self):
        config = SimpleNamespace(
            jellyfin_url="http://jellyfin.internal", jellyfin_api_key="api-key", verify_ssl=True
        )
        self.api = JellyfinAPI(config)

    def _call(self, response):
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return response

        with patch("jellyfin_stats.jellyfin_api.httpx.request", fake_request):
            return self.api.get_current_user(TOKEN), calls

    def test_asks_jellyfin_with_the_user_token_not_the_api_key(self):
        user, calls = self._call(FakeResponse(ALICE))

        self.assertEqual(user, ALICE)
        method, url, kwargs = calls[0]
        self.assertEqual((method, url), ("GET", "http://jellyfin.internal/Users/Me"))
        # Jeton seul : un Device/Version différent renommerait l'appareil
        # Jellyfin Web de l'utilisateur côté Jellyfin.
        self.assertEqual(kwargs["headers"], {"Authorization": f'MediaBrowser Token="{TOKEN}"'})

    def test_refused_or_anonymous_token_gives_none(self):
        with self.assertLogs("jellyfin_stats.jellyfin_api", level="INFO"):
            self.assertIsNone(self._call(FakeResponse({}, status_code=401))[0])
        self.assertIsNone(self._call(FakeResponse({"Name": "sans id"}))[0])
        self.assertIsNone(self._call(FakeResponse(None, status_code=204))[0])


if __name__ == "__main__":
    unittest.main()
