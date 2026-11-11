import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from jellyfin_stats import database
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app

PAYLOAD = {"NotificationType": "PlaybackStop", "UserId": "u1", "ItemId": "i1"}


class WebhookAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = Config(str(root / "config.ini"))
        self.config.set("Database", "path", str(root / "tautufin.db"))
        self.config.save()
        database.init(self.config.database_path)
        handler = patch("jellyfin_stats.activity.ActivityMonitor.handle_webhook")
        self.handle = handler.start()
        self.addCleanup(handler.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _client(self, token=""):
        self.config.set("Monitoring", "webhook_token", token)
        return TestClient(create_app(self.config))

    def test_webhook_is_disabled_without_configured_token(self):
        client = self._client("")

        response = client.post("/webhook?token=", json=PAYLOAD)

        self.assertEqual(response.status_code, 404)
        self.handle.assert_not_called()

    def test_wrong_or_missing_token_is_rejected_before_reading_the_body(self):
        client = self._client("s3cret")

        for response in (
            client.post("/webhook", json=PAYLOAD),
            client.post("/webhook?token=nope", json=PAYLOAD),
            client.post("/webhook", content=b"not json", headers={"X-Tautufin-Token": "x"}),
        ):
            self.assertEqual(response.status_code, 404)
        self.handle.assert_not_called()

    def test_token_in_header_or_query_reaches_the_monitor(self):
        client = self._client("s3cret")

        by_header = client.post("/webhook", json=PAYLOAD, headers={"X-Tautufin-Token": "s3cret"})
        by_query = client.post("/webhook?token=s3cret", json=PAYLOAD)

        self.assertEqual((by_header.status_code, by_query.status_code), (200, 200))
        self.assertEqual(self.handle.call_count, 2)
        self.handle.assert_called_with(PAYLOAD)

    def test_valid_token_with_invalid_body_returns_400(self):
        client = self._client("s3cret")

        not_json = client.post("/webhook?token=s3cret", content=b"not json")
        not_object = client.post("/webhook?token=s3cret", json=[1, 2])

        self.assertEqual((not_json.status_code, not_object.status_code), (400, 400))
        self.handle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
