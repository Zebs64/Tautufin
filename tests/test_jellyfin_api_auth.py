import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jellyfin_stats import __version__
from jellyfin_stats.jellyfin_api import JellyfinAPI


class FakeResponse:
    def __init__(self, payload=None, content=b"{}", status_code=200, headers=None):
        self._payload = payload if payload is not None else {}
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


class JellyfinAPIAuthTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            jellyfin_url="https://jellyfin.example",
            jellyfin_api_key="secret-token",
            verify_ssl=True,
        )
        self.api = JellyfinAPI(self.config)

    def test_ping_uses_standard_authorization_header_only(self):
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return FakeResponse({"ServerName": "JF", "Version": "12.0"})

        with patch("jellyfin_stats.jellyfin_api.httpx.request", fake_request):
            self.api.ping(api_key="ping-token")

        self.assertEqual(len(calls), 1)
        _, _, kwargs = calls[0]
        headers = kwargs["headers"]
        self.assertEqual(set(headers), {"Authorization"})
        self.assertIn("MediaBrowser", headers["Authorization"])
        self.assertIn(f'Version="{__version__}"', headers["Authorization"])
        self.assertIn('Token="ping-token"', headers["Authorization"])
        self.assertNotIn("X-Emby-Authorization", headers)
        self.assertNotIn("X-MediaBrowser-Authorization", headers)
        self.assertNotIn("api_key", kwargs.get("params", {}))
        self.assertNotIn("ApiKey", kwargs.get("params", {}))

    def test_binary_images_use_standard_authorization_header_only(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse(content=b"image", headers={"Content-Type": "image/png"})

        with patch("jellyfin_stats.jellyfin_api.httpx.get", fake_get):
            payload = self.api.get_image("item-1", max_width=320)

        self.assertEqual(payload, (b"image", "image/png"))
        self.assertEqual(len(calls), 1)
        _, kwargs = calls[0]
        headers = kwargs["headers"]
        self.assertEqual(set(headers), {"Authorization"})
        self.assertIn('Token="secret-token"', headers["Authorization"])
        self.assertNotIn("X-Emby-Authorization", headers)
        self.assertNotIn("X-MediaBrowser-Authorization", headers)
        self.assertEqual(kwargs["params"], {"maxWidth": 320, "quality": 90})
        self.assertNotIn("api_key", kwargs["params"])
        self.assertNotIn("ApiKey", kwargs["params"])

    def test_authenticate_by_name_does_not_persist_or_send_user_token(self):
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return FakeResponse({"User": {"Id": "u1"}, "AccessToken": "user-token"})

        with patch("jellyfin_stats.jellyfin_api.httpx.request", fake_request):
            result = self.api.authenticate_by_name("alice", "password")

        self.assertIsNotNone(result)
        self.assertEqual(result["AccessToken"], "user-token")
        _, _, kwargs = calls[0]
        headers = kwargs["headers"]
        self.assertEqual(set(headers), {"Authorization"})
        self.assertIn("MediaBrowser", headers["Authorization"])
        self.assertNotIn("Token=", headers["Authorization"])
        self.assertEqual(self.config.jellyfin_api_key, "secret-token")

    def test_iter_items_sends_auth_header_and_no_query_secret(self):
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return FakeResponse({"Items": [{"Id": "i1"}], "TotalRecordCount": 1})

        with patch("jellyfin_stats.jellyfin_api.httpx.request", fake_request):
            items = list(self.api.iter_items("library-1", page_size=10))

        self.assertEqual(items, [{"Id": "i1"}])
        _, _, kwargs = calls[0]
        headers = kwargs["headers"]
        self.assertEqual(set(headers), {"Authorization"})
        self.assertIn('Token="secret-token"', headers["Authorization"])
        params = kwargs["params"]
        self.assertEqual(params["ParentId"], "library-1")
        self.assertNotIn("api_key", params)
        self.assertNotIn("ApiKey", params)


if __name__ == "__main__":
    unittest.main()