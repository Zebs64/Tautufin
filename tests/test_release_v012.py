import unittest
from pathlib import Path

from jellyfin_stats import __version__


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "data" / "interfaces" / "default" / "templates"


class ReleaseV012Tests(unittest.TestCase):
    def test_application_remains_upgrade_compatible_with_0_1_2(self):
        self.assertGreaterEqual(tuple(map(int, __version__.split("."))), (0, 1, 2))

    def test_visible_templates_use_reglages_not_settings(self):
        occurrences = []
        for path in TEMPLATES.glob("*.html"):
            if "Settings" in path.read_text(encoding="utf-8"):
                occurrences.append(path.name)

        self.assertEqual(occurrences, [])
        self.assertIn(
            "Réglages",
            (TEMPLATES / "base.html").read_text(encoding="utf-8"),
        )

    def test_settings_routes_remain_unchanged(self):
        source = (ROOT / "jellyfin_stats" / "main.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/settings")', source)
        self.assertIn('@app.post("/api/sync")', source)


if __name__ == "__main__":
    unittest.main()
