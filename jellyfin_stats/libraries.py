"""Statistiques de bibliothèques (depuis le cache items + l'historique)."""

from . import database


def list_libraries_with_stats() -> list[dict]:
    return database.query(
        """
        SELECT l.library_id, l.name, l.collection_type, l.item_count, l.updated_at,
               COUNT(h.id) AS total_plays,
               COALESCE(SUM(h.play_duration), 0) AS total_duration
        FROM libraries l
        LEFT JOIN session_history h ON h.library_id = l.library_id
        WHERE COALESCE(l.collection_type, '') != 'boxsets'
        GROUP BY l.library_id
        ORDER BY l.name
        """
    )


def get_library(library_id: str) -> dict | None:
    return database.query_one(
        "SELECT * FROM libraries WHERE library_id = ?", (library_id,)
    )


def library_detail(library_id: str) -> dict:
    """Répartitions par genre / année / codecs + médias les plus et moins vus."""
    totals = database.query_one(
        """
        SELECT COUNT(h.id) AS total_plays,
               COALESCE(SUM(h.play_duration), 0) AS total_duration,
               COUNT(DISTINCT h.jellyfin_user_id) AS viewers
        FROM session_history h WHERE h.library_id = ?
        """,
        (library_id,),
    )
    by_genre = database.query(
        """
        SELECT je.value AS label, COUNT(*) AS count
        FROM items, json_each(items.genres) AS je
        WHERE library_id = ? AND genres IS NOT NULL
        GROUP BY je.value ORDER BY count DESC LIMIT 15
        """,
        (library_id,),
    )
    by_year = database.query(
        """
        SELECT year AS label, COUNT(*) AS count FROM items
        WHERE library_id = ? AND year IS NOT NULL
        GROUP BY year ORDER BY year
        """,
        (library_id,),
    )
    catalog = database.query(
        """
        SELECT item_id, name, type, year FROM items
        WHERE library_id = ? AND type IN ('Movie', 'Series')
        ORDER BY name COLLATE NOCASE
        """,
        (library_id,),
    )
    most_watched = database.query(
        """
        SELECT i.name, i.type, i.series_name, COUNT(h.id) AS plays,
               COALESCE(SUM(h.play_duration), 0) AS duration
        FROM items i JOIN session_history h ON h.item_id = i.item_id
        WHERE i.library_id = ?
        GROUP BY i.item_id ORDER BY plays DESC LIMIT 10
        """,
        (library_id,),
    )
    never_watched = database.query(
        """
        SELECT i.name, i.type, i.series_name, i.year
        FROM items i
        LEFT JOIN session_history h ON h.item_id = i.item_id
        WHERE i.library_id = ? AND h.id IS NULL AND i.type IN ('Movie', 'Series')
        ORDER BY i.added_at DESC LIMIT 10
        """,
        (library_id,),
    )
    return {
        "totals": totals,
        "by_genre": by_genre,
        "by_year": by_year,
        "catalog": catalog,
        "most_watched": most_watched,
        "never_watched": never_watched,
    }
