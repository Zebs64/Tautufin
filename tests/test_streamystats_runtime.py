import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jellyfin_stats import database, import_streamystats

TICKS = 10_000_000


class NoUsersAPI:
    def get_users(self):
        return []


class StreamystatsRuntimeMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tautufin.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _database_at_v10(self, rows):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (0)")
        for version, statements in database.MIGRATIONS:
            if version > 10:
                break
            for statement in statements:
                conn.execute(statement)
            conn.execute("UPDATE schema_version SET version = ?", (version,))
        conn.execute(
            "INSERT INTO items (item_id, type, runtime_seconds) VALUES ('film', 'Movie', 6000)"
        )
        conn.executemany(
            """
            INSERT INTO session_history (source, jellyfin_user_id, item_id, item_type,
                started_at, stopped_at, play_duration, runtime_seconds, percent_complete)
            VALUES (?, 'u1', ?, 'Movie', ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        conn.close()

    def test_fake_runtimes_are_replaced_or_cleared_and_others_untouched(self):
        start = "2026-03-01 20:00:00"
        self._database_at_v10(
            [
                # réelle, durée fabriquée, média connu -> corrigée
                ("import", "film", start, "2026-03-01 20:10:00", 600, 600, 100.0),
                # réelle, durée fabriquée, média inconnu -> « — »
                (
                    "import",
                    "inconnu",
                    "2026-03-02 20:00:00",
                    "2026-03-02 20:10:00",
                    600,
                    600,
                    100.0,
                ),
                # estimée (début = fin) -> inchangée
                ("import", "film", "2026-03-03 20:00:00", "2026-03-03 20:00:00", 6000, 6000, 100.0),
                # réelle, vraie durée -> inchangée
                ("import", "film", "2026-03-04 20:00:00", "2026-03-04 20:10:00", 600, 6000, 10.0),
                # capture live -> inchangée
                ("live", "film", "2026-03-05 20:00:00", "2026-03-05 20:10:00", 600, 600, 100.0),
            ]
        )

        database.init(self.db_path)

        rows = database.query(
            "SELECT runtime_seconds, percent_complete FROM session_history ORDER BY started_at"
        )
        self.assertEqual(
            [(r["runtime_seconds"], r["percent_complete"]) for r in rows],
            [(6000, 10.0), (None, None), (6000, 100.0), (6000, 10.0), (600, 100.0)],
        )


class StreamystatsImportRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        database.init(str(self.root / "tautufin.db"))
        database.execute(
            "INSERT INTO items (item_id, type, runtime_seconds) VALUES ('film', 'Movie', 6000)"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _session(self, start, duration, runtime_ticks, inferred=False, end=None):
        return {
            "userId": "u1",
            "itemId": "film",
            "startTime": start,
            "endTime": end or start,
            "playDuration": duration,
            "runtimeTicks": runtime_ticks,
            "positionTicks": runtime_ticks,
            "percentComplete": 100,
            "isInferred": inferred,
        }

    def test_fake_runtime_falls_back_to_catalog_and_inferred_stays_complete(self):
        path = self.root / "backup.json"
        path.write_text(
            json.dumps(
                {
                    "exportInfo": {"version": "streamystats"},
                    "sessions": [
                        self._session(
                            "2026-03-01 20:00:00", 600, 600 * TICKS, end="2026-03-01 20:10:00"
                        ),
                        self._session("2026-03-02 20:00:00", 6000, 6000 * TICKS, inferred=True),
                    ],
                }
            ),
            encoding="utf-8",
        )

        import_streamystats.run_import(str(path), NoUsersAPI(), 0, 0)

        rows = database.query(
            "SELECT runtime_seconds, percent_complete FROM session_history ORDER BY started_at"
        )
        self.assertEqual(
            [(r["runtime_seconds"], r["percent_complete"]) for r in rows],
            [(6000, 10.0), (6000, 100)],
        )


if __name__ == "__main__":
    unittest.main()
