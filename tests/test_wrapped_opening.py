import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from jellyfin_stats import auth, database, wrapped
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app

NOVEMBER = date(2026, 11, 11)
NEW_YEARS_EVE = date(2026, 12, 31)


class WrappedOpeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = Config(str(root / "config.ini"))
        config.set("Database", "path", str(root / "tautufin.db"))
        config.save()
        database.init(config.database_path)
        for user_id, name in (("u1", "Alice"), ("u2", "Bob")):
            database.execute(
                "INSERT INTO users (jellyfin_user_id, username) VALUES (?, ?)", (user_id, name)
            )
        self._play("u1", "2025-06-01 20:00:00")
        self._play("u1", "2026-03-01 20:00:00")
        self._play("u2", "2026-05-01 20:00:00")  # arrivé cette année
        self.client = TestClient(create_app(config))
        admin = auth.CurrentUser("t", "local", "admin", "admin", None)
        session = patch("jellyfin_stats.auth.resolve_session", return_value=admin)
        session.start()
        self.addCleanup(session.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _play(self, user_id, started):
        database.execute(
            "INSERT INTO session_history (jellyfin_user_id, item_id, item_type, item_name,"
            " started_at, play_duration) VALUES (?, ?, 'Movie', 'Film', ?, 3600)",
            (user_id, f"i-{started}", started),
        )

    def _get(self, path, today):
        with patch("jellyfin_stats.wrapped._today", return_value=today):
            return self.client.get(path)

    def test_current_year_opens_on_december_31(self):
        for today, expected in (
            (NOVEMBER, False),
            (date(2026, 12, 30), False),
            (NEW_YEARS_EVE, True),
            (date(2027, 1, 1), True),
        ):
            with patch("jellyfin_stats.wrapped._today", return_value=today):
                self.assertEqual(wrapped.is_open(2026), expected, today)

    def test_wrapped_page_hides_the_current_year_until_it_opens(self):
        page = self._get("/users/u1/wrapped", NOVEMBER)
        direct = self._get("/users/u1/wrapped?year=2026", NOVEMBER)
        opened = self._get("/users/u1/wrapped?year=2026", NEW_YEARS_EVE)

        self.assertIn("<h1>2025</h1>", page.text)
        self.assertNotIn('<option value="2026"', page.text)
        self.assertIn("disponible le <strong>31 décembre</strong>", direct.text)
        self.assertIn("J-50", direct.text)  # compte à rebours
        self.assertNotIn("J-", opened.text)
        self.assertNotIn("de visionnage", direct.text)  # aucun chiffre
        self.assertIn("<h1>2026</h1>", opened.text)
        self.assertIn("jours de visionnage cumulés", opened.text)
        # Valeurs finales dans le HTML, animées ensuite par wrapped.js.
        self.assertIn('data-count-duration="3600">1h', opened.text)
        self.assertIn("/static/js/wrapped.js", opened.text)
        self.assertNotIn("non-stop", opened.text)
        self.assertNotIn("disponible le", opened.text)

    def test_user_with_only_the_current_year_sees_the_appointment(self):
        page = self._get("/users/u2/wrapped", NOVEMBER)

        self.assertIn("disponible le <strong>31 décembre</strong>", page.text)
        self.assertNotIn("Années précédentes", page.text)

    def test_profile_card_points_to_last_open_year_with_the_appointment(self):
        alice = self._get("/users/u1", NOVEMBER).text
        bob = self._get("/users/u2", NOVEMBER).text
        bob_later = self._get("/users/u2", NEW_YEARS_EVE).text

        self.assertIn('href="/users/u1/wrapped?year=2025"', alice)
        self.assertIn("Voir le Wrapped 2025", alice)
        self.assertIn("Wrapped 2026 : rendez-vous le 31 décembre", alice)
        self.assertNotIn('href="/users/u2/wrapped', bob)  # pas de lien, rien d'ouvert
        self.assertIn("rendez-vous le <strong>31 décembre</strong>", bob)
        self.assertIn('href="/users/u2/wrapped?year=2026"', bob_later)


if __name__ == "__main__":
    unittest.main()
