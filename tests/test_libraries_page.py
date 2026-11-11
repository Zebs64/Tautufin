import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from jellyfin_stats import auth, database
from jellyfin_stats.config import Config
from jellyfin_stats.main import create_app

MOVIES, SHOWS = "a" * 32, "b" * 32
FILM, SERIES = "c" * 32, "d" * 32


class LibraryCardsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = Config(str(root / "config.ini"))
        config.set("Database", "path", str(root / "tautufin.db"))
        config.save()
        database.init(config.database_path)
        for library_id, name in ((MOVIES, "Films"), (SHOWS, "Séries")):
            database.execute(
                "INSERT INTO libraries (library_id, name, collection_type) VALUES (?, ?, 'movies')",
                (library_id, name),
            )
        for item_id, kind, series_id, day in (
            (FILM, "Movie", None, 1),
            (FILM, "Movie", None, 2),
            ("e" * 32, "Episode", SERIES, 3),
        ):
            database.execute(
                "INSERT INTO session_history (jellyfin_user_id, item_id, item_type, series_id,"
                " library_id, started_at, play_duration) VALUES ('u1', ?, ?, ?, ?, ?, 600)",
                (
                    item_id,
                    kind,
                    series_id,
                    MOVIES if kind == "Movie" else SHOWS,
                    f"2026-11-0{day} 20:00:00",
                ),
            )
        database.execute(  # bibliothèque sans lecture
            "INSERT INTO libraries (library_id, name) VALUES (?, 'Musique')", ("f" * 32,)
        )
        self.client = TestClient(create_app(config))
        admin = auth.CurrentUser("t", "local", "admin", "admin", None)
        session = patch("jellyfin_stats.auth.resolve_session", return_value=admin)
        session.start()
        self.addCleanup(session.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cards_show_the_most_watched_title_and_the_name_once(self):
        page = self.client.get("/libraries").text

        self.assertIn(f"/image/item/{FILM}?type=Backdrop", page)
        self.assertIn(f"/image/item/{SERIES}?type=Backdrop", page)  # série, pas l'épisode
        self.assertIn(f"/image/item/{'f' * 32}?w=480", page)  # repli : image Jellyfin
        self.assertEqual(page.count("<h3>Films</h3>"), 1)


if __name__ == "__main__":
    unittest.main()
