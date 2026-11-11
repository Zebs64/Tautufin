import sqlite3
import tempfile
import unittest
from pathlib import Path

from jellyfin_stats import database


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tautufin.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_populated_v4(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (0)")
        for version, statements in database.MIGRATIONS:
            if version > 4:
                break
            for statement in statements:
                conn.execute(statement)
            conn.execute("UPDATE schema_version SET version = ?", (version,))
        conn.execute(
            "INSERT INTO local_users "
            "(username, password_hash, role, jellyfin_user_id) VALUES (?, ?, ?, ?)",
            ("admin", "hash", "admin", "user-1"),
        )
        conn.execute(
            "INSERT INTO users "
            "(jellyfin_user_id, username, is_admin, hidden, access_blocked, can_view_all) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("user-1", "Alice", 1, 1, 0, 1),
        )
        conn.execute(
            "INSERT INTO libraries (library_id, name, collection_type, item_count) "
            "VALUES (?, ?, ?, ?)",
            ("lib-1", "Films", "movies", 1),
        )
        conn.execute(
            "INSERT INTO items (item_id, library_id, name, type, people) "
            "VALUES (?, ?, ?, ?, ?)",
            ("item-1", "lib-1", "Film", "Movie", '[{"Name":"Alice","Type":"Actor"}]'),
        )
        conn.execute(
            "INSERT INTO session_history "
            "(session_key, jellyfin_user_id, item_id, started_at) VALUES (?, ?, ?, ?)",
            ("session-1", "user-1", "item-1", "2026-07-30 10:00:00"),
        )
        conn.execute(
            "INSERT INTO http_sessions "
            "(token, auth_mode, username, role, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("token-1", "local", "admin", "admin", "2026-07-30 10:00:00", "2026-08-30 10:00:00"),
        )
        conn.commit()
        conn.close()

    def test_new_database_reaches_schema_v7_without_cursor(self):
        database.init(self.db_path)

        self.assertEqual(
            database.query_one("SELECT version FROM schema_version"), {"version": 7}
        )
        self.assertIsNone(database.get_sync_cursor())
        self.assertEqual(database.integrity_check()["integrity"], ["ok"])
        self.assertEqual(database.integrity_check()["foreign_key_violations"], 0)

    def test_populated_v4_migrates_without_data_loss_and_second_init_is_idempotent(self):
        self._create_populated_v4()
        tables = (
            "local_users", "users", "libraries", "items",
            "session_history", "http_sessions",
        )
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        before = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
        conn.close()

        database.init(self.db_path)
        after_upgrade = {table: database.query(f"SELECT * FROM {table}")
                         for table in tables}
        database.init(self.db_path)
        after_second_init = {table: database.query(f"SELECT * FROM {table}")
                             for table in tables}
        for snapshot in (after_upgrade, after_second_init):
            for row in snapshot["items"]:
                row.pop("source_date_last_refreshed", None)
                row.pop("source_etag", None)

        self.assertEqual(
            database.query_one("SELECT version FROM schema_version"), {"version": 7}
        )
        self.assertEqual(after_upgrade, before)
        self.assertEqual(after_second_init, before)
        self.assertIsNone(database.get_sync_cursor())
        self.assertTrue(database.integrity_check()["ok"])

    def test_reset_data_clears_sync_cursor(self):
        database.init(self.db_path)
        database.set_sync_cursor("2026-07-30T10:00:00Z")

        database.reset_data()

        self.assertIsNone(database.get_sync_cursor())


class DatabaseV6MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tautufin-v5.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_populated_v5(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (0)")
        for version, statements in database.MIGRATIONS:
            if version > 5:
                break
            for statement in statements:
                conn.execute(statement)
            conn.execute("UPDATE schema_version SET version = ?", (version,))
        conn.execute(
            "INSERT INTO items (item_id, library_id, name, type) VALUES (?, ?, ?, ?)",
            ("item-1", "lib-1", "Film conservé", "Movie"),
        )
        conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?)",
            ("items_cursor_utc", "2026-08-01T10:00:00Z"),
        )
        conn.commit()
        conn.close()

    def test_populated_v5_migrates_to_v7_without_loss_and_is_idempotent(self):
        self._create_populated_v5()

        database.init(self.db_path)
        database.init(self.db_path)

        self.assertEqual(
            database.query_one("SELECT version FROM schema_version"), {"version": 7}
        )
        self.assertEqual(
            database.query_one(
                "SELECT name, source_etag FROM items WHERE item_id = 'item-1'"
            ),
            {"name": "Film conservé", "source_etag": None},
        )
        self.assertEqual(database.get_sync_cursor(), "2026-08-01T10:00:00Z")
        self.assertTrue(database.integrity_check()["ok"])


class DatabaseV7MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tautufin-v6.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_populated_v6(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (0)")
        for version, statements in database.MIGRATIONS:
            if version > 6:
                break
            for statement in statements:
                conn.execute(statement)
            conn.execute("UPDATE schema_version SET version = ?", (version,))
        conn.execute(
            """
            INSERT INTO items
                (item_id, library_id, name, type, source_date_last_refreshed)
            VALUES ('item-1', 'lib-1', 'Film conservé', 'Movie', NULL)
            """
        )
        conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?)",
            ("items_cursor_utc", "2026-08-01T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?)",
            ("sync_health", '{"status":"success","items_enriched":12}'),
        )
        conn.commit()
        conn.close()

    def test_populated_v6_migrates_to_v7_without_loss_and_is_idempotent(self):
        self._create_populated_v6()

        database.init(self.db_path)
        database.init(self.db_path)

        self.assertEqual(database.query_one("SELECT version FROM schema_version"), {"version": 7})
        self.assertEqual(
            database.query_one(
                """
                SELECT name, source_date_last_refreshed, source_etag
                FROM items WHERE item_id = 'item-1'
                """
            ),
            {
                "name": "Film conservé",
                "source_date_last_refreshed": None,
                "source_etag": None,
            },
        )
        self.assertEqual(database.get_sync_cursor(), "2026-08-01T10:00:00Z")
        self.assertEqual(database.get_sync_health()["items_enriched"], 12)
        self.assertTrue(database.integrity_check()["ok"])


if __name__ == "__main__":
    unittest.main()
