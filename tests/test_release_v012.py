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

    def test_gitea_workflow_pushes_sha_latest_and_tag_version(self):
        workflow = (ROOT / ".gitea" / "workflows" / "docker-image.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("refs/tags/", workflow)
        self.assertIn("VERSION_TAG=", workflow)
        self.assertIn('docker push "$IMAGE:${{ gitea.sha }}"', workflow)
        self.assertIn('docker push "$IMAGE:latest"', workflow)
        self.assertIn('docker push "$IMAGE:$VERSION_TAG"', workflow)

    def test_github_workflow_keeps_versioned_tag_metadata(self):
        workflow = (ROOT / ".github" / "workflows" / "docker-image.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("type=ref,event=tag", workflow)
        self.assertIn("type=sha,prefix=sha-", workflow)
        self.assertIn("type=raw,value=latest", workflow)


if __name__ == "__main__":
    unittest.main()
