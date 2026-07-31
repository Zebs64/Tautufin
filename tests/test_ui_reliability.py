import unittest
from pathlib import Path


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

        self.assertLess(polling, unknown)
        self.assertLess(unknown, energy)
        self.assertLess(energy, health)
        self.assertIn('id="sync-health"', source)
        self.assertIn("Curseur préservé", source)

    def test_graph_pages_load_shared_state_helper_before_consumers(self):
        graphs = (TEMPLATES / "graphs.html").read_text(encoding="utf-8")
        user = (TEMPLATES / "user.html").read_text(encoding="utf-8")

        self.assertLess(graphs.index("chart-state.js"), graphs.index("graphs.js"))
        self.assertIn("chart-state.js", user)
        self.assertTrue((STATIC / "js" / "chart-state.js").is_file())

    def test_user_visible_graph_and_health_loads_have_no_silent_catch(self):
        sources = [
            (STATIC / "js" / "graphs.js").read_text(encoding="utf-8"),
            (TEMPLATES / "user.html").read_text(encoding="utf-8"),
            (TEMPLATES / "settings.html").read_text(encoding="utf-8"),
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


if __name__ == "__main__":
    unittest.main()
