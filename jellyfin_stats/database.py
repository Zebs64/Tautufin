"""SQLite : connexion, schéma et migrations versionnées.

Mécanisme : la table ``schema_version`` contient la version courante du schéma.
Au démarrage, toutes les migrations de ``MIGRATIONS`` dont le numéro est
supérieur sont appliquées dans l'ordre, chacune dans une transaction.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

logger = logging.getLogger(__name__)

_db_path: str | None = None

MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            # Comptes locaux, indépendants de Jellyfin (schéma imposé par la spec).
            """
            CREATE TABLE local_users (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                username         TEXT UNIQUE NOT NULL,
                password_hash    TEXT NOT NULL,
                role             TEXT NOT NULL DEFAULT 'user',
                jellyfin_user_id TEXT,
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_login       DATETIME
            )
            """,
            # Cache des utilisateurs Jellyfin (synchronisé depuis /Users).
            """
            CREATE TABLE users (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                jellyfin_user_id TEXT UNIQUE NOT NULL,
                username         TEXT NOT NULL,
                is_admin         INTEGER NOT NULL DEFAULT 0,
                is_active        INTEGER NOT NULL DEFAULT 1,
                last_activity    DATETIME,
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """,
            # Bibliothèques Jellyfin (/Library/VirtualFolders).
            """
            CREATE TABLE libraries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                library_id      TEXT UNIQUE NOT NULL,
                name            TEXT NOT NULL,
                collection_type TEXT,
                item_count      INTEGER NOT NULL DEFAULT 0,
                updated_at      DATETIME
            )
            """,
            # Cache des médias, pour les stats de bibliothèque et
            # l'enrichissement de l'historique (genres, durée, codecs).
            """
            CREATE TABLE items (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id          TEXT UNIQUE NOT NULL,
                library_id       TEXT,
                name             TEXT,
                type             TEXT,
                series_name      TEXT,
                season_number    INTEGER,
                episode_number   INTEGER,
                year             INTEGER,
                genres           TEXT,            -- JSON: ["Drame", ...]
                runtime_seconds  INTEGER,
                video_resolution TEXT,
                video_codec      TEXT,
                audio_codec      TEXT,
                added_at         DATETIME,
                updated_at       DATETIME
            )
            """,
            # Historique de lecture, dénormalisé pour permettre des graphiques
            # sans jointures (inspiré de session_history de Tautulli).
            """
            CREATE TABLE session_history (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key      TEXT,
                source           TEXT NOT NULL DEFAULT 'live',  -- 'live' | 'import'
                jellyfin_user_id TEXT NOT NULL,
                user_name        TEXT,
                item_id          TEXT,
                item_type        TEXT,
                item_name        TEXT,
                series_name      TEXT,
                season_number    INTEGER,
                episode_number   INTEGER,
                library_id       TEXT,
                library_name     TEXT,
                started_at       DATETIME NOT NULL,
                stopped_at       DATETIME,
                play_duration    INTEGER NOT NULL DEFAULT 0,    -- secondes réellement vues
                runtime_seconds  INTEGER,
                percent_complete REAL,
                client_name      TEXT,
                device_name      TEXT,
                ip_address       TEXT,
                play_method      TEXT,                          -- DirectPlay | DirectStream | Transcode
                video_resolution TEXT,
                video_codec      TEXT,
                audio_codec      TEXT,
                genres           TEXT,                          -- JSON
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """,
            # Sessions HTTP côté serveur : le cookie ne contient que le token
            # signé. Aucun token Jellyfin n'est stocké ici (exigence spec).
            """
            CREATE TABLE http_sessions (
                token            TEXT PRIMARY KEY,
                auth_mode        TEXT NOT NULL,                 -- 'jellyfin' | 'local'
                username         TEXT NOT NULL,
                role             TEXT NOT NULL,
                jellyfin_user_id TEXT,
                created_at       DATETIME NOT NULL,
                expires_at       DATETIME NOT NULL,
                last_active      DATETIME
            )
            """,
        ],
    ),
    (
        2,
        [
            # Index de requêtage pour l'historique et les graphiques.
            "CREATE INDEX idx_history_started ON session_history(started_at)",
            "CREATE INDEX idx_history_user ON session_history(jellyfin_user_id, started_at)",
            "CREATE INDEX idx_history_item ON session_history(item_id)",
            "CREATE INDEX idx_history_type ON session_history(item_type, started_at)",
            # Clé de déduplication de l'import Playback Reporting
            # (DateCreated + UserId + ItemId) → INSERT OR IGNORE = idempotence.
            """
            CREATE UNIQUE INDEX idx_history_dedup
                ON session_history(jellyfin_user_id, item_id, started_at)
            """,
            "CREATE INDEX idx_items_library ON items(library_id)",
            "CREATE INDEX idx_sessions_expires ON http_sessions(expires_at)",
        ],
    ),
    (
        3,
        [
            # Distribution / équipe d'un média (JSON: [{"Name":..,"Type":..}, ...]),
            # renseignée à la synchro pour les tops acteurs / réalisateurs.
            "ALTER TABLE items ADD COLUMN people TEXT",
        ],
    ),
]


def now_iso() -> str:
    """Horodatage au format compris par les fonctions de date SQLite."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init(path: str) -> None:
    global _db_path
    _db_path = path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        current = row["version"] if row else 0
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (0)")
        for version, statements in MIGRATIONS:
            if version <= current:
                continue
            logger.info("Migration du schéma vers la version %d", version)
            for sql in statements:
                conn.execute(sql)
            conn.execute("UPDATE schema_version SET version = ?", (version,))


@contextmanager
def db():
    """Connexion par opération : commit en sortie, rollback sur exception."""
    if _db_path is None:
        raise RuntimeError("database.init() n'a pas été appelé")
    conn = sqlite3.connect(_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params=()) -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params=()) -> dict | None:
    with db() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def execute(sql: str, params=()) -> int:
    """Exécute une écriture, retourne le nombre de lignes affectées."""
    with db() as conn:
        return conn.execute(sql, params).rowcount
