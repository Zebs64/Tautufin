"""SQLite : connexion, schéma et migrations versionnées.

Mécanisme : la table ``schema_version`` contient la version courante du schéma.
Au démarrage, toutes les migrations de ``MIGRATIONS`` dont le numéro est
supérieur sont appliquées dans l'ordre, chacune dans une transaction.
"""

import json
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
                play_method      TEXT,                          -- DirectPlay|DirectStream|Transcode
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
    (
        4,
        [
            # Gestion fine des utilisateurs Jellyfin :
            #  - hidden         : exclu de toutes les stats globales
            #  - access_blocked : connexion à Tautufin refusée
            #  - can_view_all   : droit « vision » (voit les stats de tous,
            #                     sans accès à la configuration)
            "ALTER TABLE users ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN access_blocked INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN can_view_all INTEGER NOT NULL DEFAULT 0",
        ],
    ),
    (
        5,
        [
            # État persistant des synchronisations. Une ligne absente signifie
            # qu'une full sync d'amorçage est requise.
            """
            CREATE TABLE sync_state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
        ],
    ),
    (
        6,
        [
            # Marqueur source stable utilisé par la synchronisation en deux passes.
            # Nullable : le premier cycle après migration enrichit le média.
            "ALTER TABLE items ADD COLUMN source_date_last_refreshed TEXT",
        ],
    ),
    (
        7,
        [
            # ETag réellement exposé par Jellyfin 10.11.11. La colonne v6 est
            # conservée pour permettre un rollback applicatif sans perte.
            "ALTER TABLE items ADD COLUMN source_etag TEXT",
        ],
    ),
    (
        8,
        [
            # Lectures en cours (TrackedSession sérialisée en JSON), recopiées à
            # chaque cycle de polling pour survivre à un redémarrage.
            """
            CREATE TABLE active_sessions (
                session_key TEXT PRIMARY KEY,
                state       TEXT NOT NULL,
                updated_at  DATETIME NOT NULL
            )
            """,
        ],
    ),
    (
        9,
        [
            # Série d'un épisode par identifiant, et non plus par nom (deux
            # séries peuvent porter le même nom).
            "ALTER TABLE items ADD COLUMN series_id TEXT",
            "ALTER TABLE session_history ADD COLUMN series_id TEXT",
            # La synchro saute les ETag inchangés : forcer un réenrichissement
            # des épisodes pour remplir items.series_id au prochain cycle.
            "UPDATE items SET source_etag = NULL WHERE type = 'Episode'",
        ],
    ),
    (
        10,
        [
            # Média disparu de l'inventaire Jellyfin de sa bibliothèque. La
            # ligne est conservée (l'historique y fait référence) ; seuls les
            # catalogues l'excluent.
            "ALTER TABLE items ADD COLUMN removed_at DATETIME",
        ],
    ),
    (
        11,
        [
            # Les backups Streamystats donnent, pour une partie des sessions
            # réelles, une durée de média égale au temps vu (et 100 %). On
            # reprend la durée du catalogue quand elle est connue, sinon on
            # efface durée et % (affichés « — ») plutôt qu'un faux 100 %.
            # Sessions estimées (début = fin) non concernées.
            """
            UPDATE session_history
            SET runtime_seconds = (
                    SELECT i.runtime_seconds FROM items i
                    WHERE i.item_id = session_history.item_id AND i.runtime_seconds > 0
                ),
                percent_complete = MIN(100.0, play_duration * 100.0 / (
                    SELECT i.runtime_seconds FROM items i
                    WHERE i.item_id = session_history.item_id AND i.runtime_seconds > 0
                ))
            WHERE source = 'import'
              AND stopped_at > started_at
              AND ABS(runtime_seconds - play_duration) <= 1
            """,
        ],
    ),
]


# Écart toléré entre une lecture importée et la même lecture capturée en live :
# le live date la session au premier polling (à poll_interval près), la source
# importée à son propre horodatage.
LIVE_OVERLAP_SECONDS = 120


def has_live_play_near(conn, jellyfin_user_id: str, item_id: str, started_at: str) -> bool:
    """Une lecture capturée en live existe-t-elle déjà pour ce couple
    (utilisateur, média) à ±LIVE_OVERLAP_SECONDS ? Seule la source 'live' est
    comparée : les lignes d'une même source importée peuvent légitimement être
    proches (Playback Reporting écrit un segment par reprise après pause) et ne
    doivent pas être fusionnées. Requête servie par l'index de dédoublonnage."""
    delta = f"{LIVE_OVERLAP_SECONDS} seconds"
    return (
        conn.execute(
            """
            SELECT 1 FROM session_history
            WHERE jellyfin_user_id = ? AND item_id = ? AND source = 'live'
              AND started_at BETWEEN datetime(?, '-' || ?) AND datetime(?, '+' || ?)
            LIMIT 1
            """,
            (jellyfin_user_id, item_id, started_at, delta, started_at, delta),
        ).fetchone()
        is not None
    )


def year_bounds(year: int) -> tuple[str, str]:
    """Bornes [1er janvier, 1er janvier suivant) d'une année civile, au format
    texte de ``started_at``. Comparer la colonne nue à ces bornes permet à
    SQLite d'utiliser ses index, contrairement à ``strftime('%Y', ...)``."""
    y = int(year)
    return f"{y:04d}-01-01", f"{y + 1:04d}-01-01"


def now_iso() -> str:
    """Horodatage au format compris par les fonctions de date SQLite."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init(path: str) -> None:
    global _db_path
    _db_path = path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        current = row["version"] if row else 0
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (0)")
        conn.commit()
        # Une transaction explicite par migration : le module sqlite3 n'en
        # ouvre pas avant un CREATE/ALTER, et une migration interrompue
        # laisserait sinon un schéma partiel impossible à rejouer.
        for version, statements in MIGRATIONS:
            if version <= current:
                continue
            logger.info("Migration du schéma vers la version %d", version)
            conn.execute("BEGIN")
            for sql in statements:
                conn.execute(sql)
            conn.execute("UPDATE schema_version SET version = ?", (version,))
            conn.commit()


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


# --- État de synchronisation -----------------------------------------------

_SYNC_HEALTH_KEY = "sync_health"
# Clé des versions <= 0.3.2, qui en déduisaient le mode de synchronisation.
# Elle n'est plus écrite ni lue ; seule la réinitialisation l'efface, pour qu'un
# retour à une ancienne version reparte d'une synchro complète après un reset.
_LEGACY_SYNC_CURSOR_KEY = "items_cursor_utc"


def _upsert_sync_state(conn, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def empty_sync_health() -> dict:
    """État stable renvoyé avant la première tentative de synchronisation."""
    return {
        "status": "never",
        "mode": None,
        "phase": "idle",
        "started_at": None,
        "last_attempt_at": None,
        "last_success_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "users": 0,
        "libraries": 0,
        "items_received": 0,
        "items_inspected": 0,
        "items_enriched": 0,
        "items_changed": 0,
        "error": None,
    }


def get_sync_health() -> dict:
    row = query_one("SELECT value FROM sync_state WHERE key = ?", (_SYNC_HEALTH_KEY,))
    if not row:
        return empty_sync_health()
    try:
        value = json.loads(row["value"])
        if not isinstance(value, dict):
            raise ValueError("le document n'est pas un objet JSON")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("État de santé de synchronisation invalide, ignoré : %s", exc)
        return empty_sync_health()
    health = empty_sync_health()
    health.update(value)
    return health


def set_sync_health(value: dict) -> None:
    health = empty_sync_health()
    health.update(value)
    serialized = json.dumps(health, ensure_ascii=False, separators=(",", ":"))
    with db() as conn:
        _upsert_sync_state(conn, _SYNC_HEALTH_KEY, serialized)


def clear_sync_health() -> None:
    execute("DELETE FROM sync_state WHERE key = ?", (_SYNC_HEALTH_KEY,))


# --- Maintenance -----------------------------------------------------------

# Tables de données (caches + historique) ; vidées par un reset. Les tables
# d'authentification (local_users, http_sessions) et schema_version sont
# préservées pour ne pas verrouiller l'admin hors de l'application.
_DATA_TABLES = ("session_history", "items", "libraries", "users")


def backup_bytes() -> bytes:
    """Snapshot cohérent de la base (API backup SQLite, prend en compte le WAL)
    renvoyé en mémoire pour téléchargement."""
    import tempfile

    if _db_path is None:
        raise RuntimeError("database.init() n'a pas été appelé")
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(_db_path, timeout=30)
        try:
            dest = sqlite3.connect(tmp)
            try:
                src.backup(dest)
            finally:
                dest.close()
        finally:
            src.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.remove(tmp)


def integrity_check() -> dict:
    """Vérifie l'intégrité de la base (PRAGMA integrity_check + clés
    étrangères) et renvoie quelques compteurs utiles."""
    with db() as conn:
        integrity = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
        fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in _DATA_TABLES + ("local_users",)
        }
    size = os.path.getsize(_db_path) if _db_path and os.path.exists(_db_path) else 0
    return {
        "ok": integrity == ["ok"] and fk_violations == 0,
        "integrity": integrity,
        "foreign_key_violations": fk_violations,
        "counts": counts,
        "size_bytes": size,
    }


def backfill_history_series() -> int:
    """Renseigne ``session_history.series_id`` depuis le catalogue pour les
    épisodes qui ne l'ont pas encore (lectures antérieures à la v0.3.0,
    imports). Idempotent ; renvoie le nombre de lignes complétées."""
    return execute(
        """
        UPDATE session_history
        SET series_id = (
            SELECT i.series_id FROM items i WHERE i.item_id = session_history.item_id
        )
        WHERE series_id IS NULL
          AND item_type = 'Episode'
          AND item_id IN (SELECT item_id FROM items WHERE series_id IS NOT NULL)
        """
    )


def reset_data() -> dict:
    """Vide les tables de données (historique + caches). Préserve les comptes
    locaux et les sessions HTTP. Renvoie le nombre de lignes supprimées."""
    deleted = {}
    with db() as conn:
        for table in _DATA_TABLES:
            deleted[table] = conn.execute(f"DELETE FROM {table}").rowcount
        conn.execute(
            "DELETE FROM sync_state WHERE key IN (?, ?)",
            (_LEGACY_SYNC_CURSOR_KEY, _SYNC_HEALTH_KEY),
        )
    # VACUUM doit s'exécuter hors transaction.
    conn = sqlite3.connect(_db_path, timeout=30)
    conn.isolation_level = None
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    return deleted
