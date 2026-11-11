import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from jellyfin_stats import auth, database
from jellyfin_stats.config import Config
from jellyfin_stats.main import NEGATIVE_IMAGE_CACHE_SECONDS, create_app

ITEM_ID = "0123456789abcdef0123456789abcdef"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class NegativeImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = Config(str(root / "config.ini"))
        config.set("Database", "path", str(root / "tautufin.db"))
        config.set("Jellyfin", "url", "http://jellyfin.invalid")
        config.set("Jellyfin", "api_key", "key")
        config.save()
        database.init(config.database_path)
        self.cache_file = root / "cache" / "images" / f"item_{ITEM_ID}_480"
        self.client = TestClient(create_app(config))
        user = auth.CurrentUser("token", "local", "admin", "admin", None)
        session = patch("jellyfin_stats.auth.resolve_session", return_value=user)
        session.start()
        self.addCleanup(session.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _get(self, image):
        with patch(
            "jellyfin_stats.jellyfin_api.JellyfinAPI.get_image", return_value=image
        ) as get_image:
            response = self.client.get(f"/image/item/{ITEM_ID}?fallback={ITEM_ID}")
        return response, get_image.call_count

    def test_missing_image_is_cached_then_refetched_once_expired(self):
        first, calls = self._get(None)
        self.assertEqual(calls, 1)
        self.assertEqual(first.headers["content-type"], "image/svg+xml")
        self.assertEqual(self.cache_file.read_bytes(), b"")

        _, calls = self._get((PNG, "image/png"))
        self.assertEqual(calls, 0)  # négatif encore frais : pas de requête

        old = time.time() - NEGATIVE_IMAGE_CACHE_SECONDS - 60
        os.utime(self.cache_file, (old, old))
        refreshed, calls = self._get((PNG, "image/png"))

        self.assertEqual(calls, 1)
        self.assertEqual(refreshed.content, PNG)
        self.assertEqual(self.cache_file.read_bytes(), PNG)

    def test_cached_image_never_expires(self):
        self._get((PNG, "image/png"))
        old = time.time() - NEGATIVE_IMAGE_CACHE_SECONDS * 10
        os.utime(self.cache_file, (old, old))

        response, calls = self._get(None)

        self.assertEqual(calls, 0)
        self.assertEqual(response.content, PNG)

    def test_episode_without_image_uses_its_series_by_id(self):
        series_id = "fedcba9876543210fedcba9876543210"
        database.execute(
            "INSERT INTO items (item_id, type, name, series_name, series_id) VALUES "
            "(?, 'Episode', 'Pilot', 'The Office', ?)",
            (ITEM_ID, series_id),
        )

        def fake_image(item_id, **kwargs):
            return (PNG, "image/png") if item_id == series_id else None

        with patch("jellyfin_stats.jellyfin_api.JellyfinAPI.get_image", side_effect=fake_image):
            response = self.client.get(f"/image/item/{ITEM_ID}")

        self.assertEqual(response.content, PNG)


if __name__ == "__main__":
    unittest.main()
