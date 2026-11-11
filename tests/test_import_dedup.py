import json
import tempfile
import unittest
from pathlib import Path

from jellyfin_stats import database, import_playback, import_streamystats


class NoUsersAPI:
    def get_users(self):
        return []


def tsv_line(started, user, item, duration, name="Film"):
    return "\t".join([started, user, item, "Movie", name, "DirectPlay", "Web", "PC", str(duration)])


class ImportLiveOverlapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        database.init(str(self.root / "tautufin.db"))
        # Lecture capturée en live : le polling l'a datée 10 s après son début.
        database.execute(
            """
            INSERT INTO session_history
                (source, jellyfin_user_id, item_id, item_type, item_name, started_at, play_duration)
            VALUES ('live', 'u1', 'i1', 'Movie', 'Film', '2026-11-01 20:00:10', 5400)
            """
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _sources(self):
        return database.query(
            "SELECT source, item_id, started_at FROM session_history ORDER BY started_at, id"
        )

    def _run_tsv(self, lines):
        path = self.root / "PlaybackReportingBackup.tsv"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return import_playback.run_import(str(path), NoUsersAPI(), 0, 0)

    def test_playback_reporting_row_matching_a_live_play_is_skipped(self):
        report = self._run_tsv(
            [
                tsv_line("2026-11-01 20:00:00", "u1", "i1", 5390),  # même lecture que le live
                tsv_line("2026-11-01 20:05:00", "u1", "i1", 60),  # hors fenêtre : gardée
                tsv_line("2026-11-01 20:00:00", "u2", "i1", 5390),  # autre utilisateur
            ]
        )

        self.assertEqual((report["imported"], report["duplicates"]), (2, 1))
        self.assertEqual(
            [(r["source"], r["started_at"]) for r in self._sources()],
            [
                ("import", "2026-11-01 20:00:00"),  # u2
                ("live", "2026-11-01 20:00:10"),
                ("import", "2026-11-01 20:05:00"),
            ],
        )

    def test_close_segments_of_the_same_import_are_all_kept(self):
        # Playback Reporting écrit un segment par reprise après pause.
        lines = [
            tsv_line("2026-11-02 21:00:00", "u1", "i2", 900),
            tsv_line("2026-11-02 21:03:00", "u1", "i2", 1200),
        ]

        first = self._run_tsv(lines)
        second = self._run_tsv(lines)

        self.assertEqual((first["imported"], first["duplicates"]), (2, 0))
        self.assertEqual((second["imported"], second["duplicates"]), (0, 2))
        self.assertEqual(
            [r["started_at"] for r in self._sources() if r["item_id"] == "i2"],
            ["2026-11-02 21:00:00", "2026-11-02 21:03:00"],
        )

    def test_streamystats_session_matching_a_live_play_is_skipped(self):
        path = self.root / "streamystats.json"
        path.write_text(
            json.dumps(
                {
                    "exportInfo": {"version": "streamystats"},
                    "sessions": [
                        {
                            "userId": "u1",
                            "itemId": "i1",
                            "itemName": "Film",
                            "itemType": "Movie",
                            "startTime": "2026-11-01 19:59:30",
                            "playDuration": 5400,
                        },
                        {
                            "userId": "u1",
                            "itemId": "i3",
                            "itemName": "Autre",
                            "itemType": "Movie",
                            "startTime": "2026-11-01 19:59:30",
                            "playDuration": 600,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        report = import_streamystats.run_import(str(path), NoUsersAPI(), 0, 0)

        self.assertEqual((report["imported"], report["duplicates"]), (1, 1))
        self.assertEqual(
            sorted(r["item_id"] for r in self._sources() if r["source"] == "import"), ["i3"]
        )


if __name__ == "__main__":
    unittest.main()
