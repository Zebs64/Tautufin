"""Mode intégré (onglet Custom Tabs de Jellyfin) : détection côté navigateur,
origines Jellyfin transmises aux pages, extrait de l'onglet dans les réglages."""

import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from jellyfin_stats import auth, database
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app
from jellyfin_stats.web.helpers import TEMPLATES_DIR

ORIGIN = "http://tautufin.local"


class EmbedModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = Config(str(root / "config.ini"))
        self.config.set("Database", "path", str(root / "tautufin.db"))
        self.config.save()
        database.init(self.config.database_path)
        auth.create_local_user("admin", "secret", "admin")
        self.client = TestClient(create_app(self.config), base_url=ORIGIN)
        self.client.post(
            "/login",
            data={"mode": "local", "username": "admin", "password": "secret"},
            headers={"Origin": ORIGIN},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_page_detects_the_iframe_before_rendering(self):
        anonymous = TestClient(self.client.app, base_url=ORIGIN)
        pages = {
            "/": self.client.get("/").text,
            "/history": self.client.get("/history").text,
            "/login": anonymous.get("/login").text,
        }
        for path, page in pages.items():
            head = page.split("</head>")[0]
            self.assertIn("window.self !== window.top", head, path)
            self.assertIn("/static/js/embed.js", head, path)

    def test_pages_report_navigation_only_to_configured_origins(self):
        self.assertNotIn("data-embed-origins", self.client.get("/").text)

        self.config.set("Auth", "jellyfin_web_origins", "https://jellyfin.example")
        page = self.client.get("/history").text
        self.assertIn('data-embed-origins="https://jellyfin.example"', page)

        anonymous = TestClient(self.client.app, base_url=ORIGIN).get("/login").text
        self.assertNotIn("data-embed-origins", anonymous)

    def test_account_menu_keeps_logout_reachable(self):
        page = self.client.get("/").text
        menu = page.split('<details class="nav-account">')[1].split("</details>")[0]
        self.assertIn('href="/profile"', menu)
        self.assertIn('action="/logout"', menu)

    def test_settings_offer_the_tab_snippet_escaped(self):
        page = self.client.get("/settings").text
        textarea = re.search(r'<textarea id="tab-snippet"[^>]*>(.*?)</textarea>', page, re.S)
        self.assertIsNotNone(textarea)
        content = textarea.group(1)
        self.assertIn("&lt;iframe class=&#34;tautufin-tab-frame&#34;", content)
        self.assertIn("__TAUTUFIN_ORIGIN__", content)
        self.assertNotIn("<script", content)
        self.assertIn("Renseignez ci-dessus l'adresse publique de Jellyfin Web", page)

        self.config.set("Auth", "jellyfin_web_origins", "https://jellyfin.example")
        self.assertNotIn("Renseignez ci-dessus", self.client.get("/settings").text)


class TabSnippetTests(unittest.TestCase):
    def setUp(self):
        self.snippet = (TEMPLATES_DIR / "_jellyfin_tab.html").read_text(encoding="utf-8")

    def test_snippet_has_no_hardcoded_offset_and_a_transparent_frame(self):
        self.assertIn("var(--tautufin-top", self.snippet)
        self.assertNotIn("@media", self.snippet)
        self.assertIn("background: transparent; color-scheme: dark;", self.snippet)

    def test_token_and_pages_only_go_to_tautufin(self):
        self.assertIn("event.origin !== TAUTUFIN", self.snippet)
        self.assertIn(
            "postMessage({ type: 'tautufin:jellyfin-session', token: token }, TAUTUFIN)",
            self.snippet,
        )
        # Chemin mémorisé : relatif à Tautufin, jamais « //autre-site ».
        self.assertIn(r"/^\/(?!\/)[^\s\\]{0,2000}$/", self.snippet)

    def test_snippet_is_plain_html_without_jinja(self):
        for marker in ("{{", "{%", "{#"):
            self.assertNotIn(marker, self.snippet)


if __name__ == "__main__":
    unittest.main()
