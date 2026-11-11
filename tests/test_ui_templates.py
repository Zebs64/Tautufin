import re
import unittest
from pathlib import Path

from jellyfin_stats.web.helpers import LIBRARY_ICONS, MEDIA_ICONS

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "data" / "interfaces" / "default" / "templates"
STATIC = ROOT / "data" / "interfaces" / "default" / "static"


class ReliabilityUIContractTests(unittest.TestCase):
    def test_settings_contains_sync_health_card_in_expected_grid_order(self):
        source = (TEMPLATES / "settings.html").read_text(encoding="utf-8")

        polling = source.index("Polling &amp; synchronisation")
        unknown = source.index("Clients inconnus")
        energy = source.index("Estimation énergie (transcodage)")
        health = source.index("État de la synchronisation")

        self.assertLess(polling, health)
        self.assertLess(health, unknown)
        self.assertLess(unknown, energy)
        self.assertIn('id="sync-health"', source)
        self.assertIn("Médias inspectés", source)
        self.assertIn("Médias enrichis", source)

    def test_graph_pages_load_shared_state_helper_before_consumers(self):
        graphs = (TEMPLATES / "graphs.html").read_text(encoding="utf-8")
        user = (TEMPLATES / "user.html").read_text(encoding="utf-8")

        self.assertLess(graphs.index("chart-state.js"), graphs.index("graphs.js"))
        self.assertIn("chart-state.js", user)
        self.assertTrue((STATIC / "js" / "chart-state.js").is_file())

    def test_user_visible_graph_and_health_loads_have_no_silent_catch(self):
        sources = [
            (STATIC / "js" / name).read_text(encoding="utf-8")
            for name in ("graphs.js", "user.js", "settings.js")
        ]

        for source in sources:
            self.assertNotIn(".catch(() => {})", source)
        helper = (STATIC / "js" / "chart-state.js").read_text(encoding="utf-8")
        self.assertIn("console.error", helper)
        self.assertIn("Réessayer", helper)
        self.assertIn("Chargement", helper)
        self.assertIn("Aucune donnée", helper)

    def test_history_exposes_reset_chips_total_and_retry_state(self):
        template = (TEMPLATES / "history.html").read_text(encoding="utf-8")
        script = (STATIC / "js" / "history.js").read_text(encoding="utf-8")

        for marker in ('id="history-reset"', 'id="history-chips"', 'id="history-total"'):
            self.assertIn(marker, template)
        self.assertIn("window.addEventListener('popstate'", script)
        self.assertIn("history.pushState", script)
        self.assertIn("history.replaceState", script)
        self.assertIn("Réessayer", script)
        self.assertIn("tbody.innerHTML", script)

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

    def test_templates_load_scripts_from_the_app_only(self):
        for path in TEMPLATES.glob("*.html"):
            source = path.read_text(encoding="utf-8")
            for src in re.findall(r'<script[^>]+src="([^"]+)"', source):
                self.assertTrue(src.startswith("/static/"), f"{path.name}: {src}")
                file = src.removeprefix("/static/").split("?")[0]
                self.assertTrue((STATIC / file).is_file(), src)
                if "/vendor/" not in src:  # versionnés : le cache suit les mises à jour
                    self.assertTrue(src.endswith("?v={{ version }}"), f"{path.name}: {src}")


class IconSpriteTests(unittest.TestCase):
    """Icônes SVG du sprite Material : aucune référence cassée, plus d'émoji."""

    # Plages d'\u00e9mojis, plus tout caract\u00e8re suivi du s\u00e9lecteur de pr\u00e9sentation
    # \u00e9moji (U+FE0F), comme \u00ab \u21a9\ufe0f \u00bb.
    EMOJI = re.compile("[\U0001f300-\U0001faff\u2600-\u27bf\u2b50\u23e9-\u23fa\u2630]|.\ufe0f")

    @staticmethod
    def _arguments(source, start):
        """Texte entre la parenthèse ouvrante (déjà passée) et sa fermante."""
        depth = 1
        for i in range(start, len(source)):
            depth += {"(": 1, ")": -1}.get(source[i], 0)
            if depth == 0:
                return source[start:i]
        return ""

    def _sources(self):
        files = [*TEMPLATES.glob("*.html"), *(STATIC / "js").glob("*.js")]
        files += [ROOT / "jellyfin_stats" / "users.py"]
        return {f.name: f.read_text(encoding="utf-8") for f in files}

    def test_every_referenced_icon_exists_in_the_sprite(self):
        sprite = (STATIC / "img" / "icons.svg").read_text(encoding="utf-8")
        symbols = set(re.findall(r'<symbol id="([a-z_]+)"', sprite))
        used = set(MEDIA_ICONS.values()) | set(LIBRARY_ICONS.values()) | {"play_circle", "folder"}
        for source in self._sources().values():
            for call in re.finditer(r"\b(?:icon|setButtonLabel)\(", source):
                used |= set(re.findall(r"'([a-z_]+)'", self._arguments(source, call.end())))
            used |= set(re.findall(r"icon: '([a-z_]+)'", source))
            used |= set(re.findall(r'\("[^"]+", "([a-z_]+)", \w+\)', source))  # users.py
        js_libraries = re.search(r"LIBRARY_ICONS = \{(.*?)\};", self._sources()["home.js"], re.S)
        used |= set(re.findall(r": '([a-z_]+)'", js_libraries.group(1)))
        self.assertEqual(sorted(used - symbols), [])

    def test_no_emoji_left_in_the_interface(self):
        for name, source in self._sources().items():
            self.assertEqual(self.EMOJI.findall(source), [], name)


if __name__ == "__main__":
    unittest.main()
