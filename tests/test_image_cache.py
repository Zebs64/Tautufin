import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from jellyfin_stats import auth, database, image_cache
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app
from jellyfin_stats.scheduler import Scheduler
from jellyfin_stats.web.helpers import NEGATIVE_IMAGE_CACHE_SECONDS

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


class ImageCacheMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "cache" / "images"
        self.dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, size, age):
        path = self.dir / name
        path.write_bytes(b"x" * size)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_trim_removes_oldest_files_first_and_only_what_is_needed(self):
        self._write("old", 400, age=300)
        self._write("middle", 400, age=200)
        self._write("recent", 400, age=100)
        self._write("negative", 0, age=400)  # absence mise en cache (0 octet)

        removed = image_cache.trim(self.dir, max_bytes=800)

        self.assertEqual(removed, 2)  # « negative » puis « old »
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["middle", "recent"])
        self.assertEqual(image_cache.trim(self.dir, max_bytes=800), 0)

    def test_clear_reports_freed_files_and_bytes(self):
        self._write("a", 100, age=10)
        self._write("b", 50, age=10)

        self.assertEqual(image_cache.clear(self.dir), {"files": 2, "bytes": 150})
        self.assertEqual(list(self.dir.iterdir()), [])
        self.assertEqual(
            image_cache.clear(Path(self.tmp.name) / "absent"), {"files": 0, "bytes": 0}
        )

    def test_clear_route_is_admin_only(self):
        config = Config(str(Path(self.tmp.name) / "config.ini"))
        config.set("Database", "path", str(Path(self.tmp.name) / "tautufin.db"))
        config.save()
        database.init(config.database_path)
        self._write("poster", 100, age=10)
        client = TestClient(create_app(config))
        viewer = auth.CurrentUser("t", "local", "bob", "user", None)
        admin = auth.CurrentUser("t", "local", "admin", "admin", None)

        with patch("jellyfin_stats.auth.resolve_session", return_value=viewer):
            self.assertEqual(client.post("/api/settings/clear-image-cache").status_code, 403)
        with patch("jellyfin_stats.auth.resolve_session", return_value=admin):
            response = client.post("/api/settings/clear-image-cache")

        self.assertEqual(response.json(), {"files": 1, "bytes": 100})

    def test_hourly_cleanup_trims_the_cache(self):
        config = SimpleNamespace(path=str(Path(self.tmp.name) / "config.ini"))
        scheduler = Scheduler(config, object(), object())

        async def one_cycle():
            with patch("jellyfin_stats.scheduler.auth.purge_expired_sessions", return_value=0):
                with patch("jellyfin_stats.scheduler.image_cache.trim") as trim:
                    with patch(
                        "jellyfin_stats.scheduler.asyncio.sleep",
                        side_effect=asyncio.CancelledError,
                    ):
                        with self.assertRaises(asyncio.CancelledError):
                            await scheduler._cleanup_loop()
            return trim

        trim = asyncio.run(one_cycle())
        trim.assert_called_once_with(self.dir)


if __name__ == "__main__":
    unittest.main()
