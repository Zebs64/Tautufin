import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from jellyfin_stats import auth, database
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app


class HistoryExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = Config(str(root / "config.ini"))
        config.set("Database", "path", str(root / "tautufin.db"))
        config.save()
        database.init(config.database_path)
        for user_id, item, started, device, duration in (
            ("u1", "i1", "2026-11-01 20:00:00", '=HYPERLINK("http://x")', 3600),
            ("u1", "i2", "2026-11-03 21:00:00", "-salon", 1200),
            ("u2", "i3", "2026-11-02 19:00:00", "TV", 600),
        ):
            database.execute(
                """
                INSERT INTO session_history
                    (jellyfin_user_id, user_name, item_id, item_type, item_name, started_at,
                     play_duration, percent_complete, device_name, client_name, ip_address)
                VALUES (?, ?, ?, 'Movie', ?, ?, ?, 87.654, ?, 'Web', '192.0.2.1')
                """,
                (user_id, user_id.upper(), item, f"Film {item}", started, duration, device),
            )
        self.client = TestClient(create_app(config))

    def tearDown(self):
        self.tmp.cleanup()

    def _export(self, user, query=""):
        with patch("jellyfin_stats.auth.resolve_session", return_value=user):
            response = self.client.get("/api/history.csv" + query)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertTrue(response.text.startswith("﻿"))
        return list(csv.DictReader(io.StringIO(response.text[1:]), delimiter=";"))

    def test_admin_export_follows_filters_and_sort(self):
        admin = auth.CurrentUser("t", "local", "admin", "admin", None)

        rows = self._export(admin, "?date_from=2026-11-02&sort=date&order=asc")

        self.assertEqual([r["item_name"] for r in rows], ["Film i3", "Film i2"])
        self.assertEqual(rows[0]["ip_address"], "192.0.2.1")
        self.assertEqual(rows[0]["percent_complete"], "87.7")

    def test_regular_user_exports_only_own_plays_without_ip(self):
        bob = auth.CurrentUser("t", "jellyfin", "bob", "user", "u1")

        rows = self._export(bob, "?user_id=u2")

        self.assertEqual(sorted(r["item_name"] for r in rows), ["Film i1", "Film i2"])
        self.assertNotIn("ip_address", rows[0])

    def test_spreadsheet_formulas_are_neutralized(self):
        admin = auth.CurrentUser("t", "local", "admin", "admin", None)

        devices = {r["item_name"]: r["device_name"] for r in self._export(admin)}

        self.assertEqual(devices["Film i1"], '\'=HYPERLINK("http://x")')
        self.assertEqual(devices["Film i2"], "'-salon")
        self.assertEqual(devices["Film i3"], "TV")


if __name__ == "__main__":
    unittest.main()
