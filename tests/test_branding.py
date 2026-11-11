import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from jellyfin_stats import database
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app

MALICIOUS_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


class BrandingHeadersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = Config(str(root / "config.ini"))
        config.set("Database", "path", str(root / "tautufin.db"))
        config.save()
        database.init(config.database_path)
        self.branding = root / "branding"
        self.client = TestClient(create_app(config))

    def tearDown(self):
        self.tmp.cleanup()

    def _assert_scripts_blocked(self, response):
        self.assertEqual(response.status_code, 200)
        csp = response.headers["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("script-src", csp)  # donc aucun script autorisé
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_uploaded_svg_logo_cannot_run_scripts(self):
        self.branding.mkdir()
        (self.branding / "logo.svg").write_bytes(MALICIOUS_SVG)

        response = self.client.get("/branding/logo")

        self.assertEqual(response.content, MALICIOUS_SVG)
        self.assertIn("image/svg+xml", response.headers["content-type"])
        self._assert_scripts_blocked(response)

    def test_default_favicon_gets_the_same_protection(self):
        self._assert_scripts_blocked(self.client.get("/favicon.ico"))


if __name__ == "__main__":
    unittest.main()
