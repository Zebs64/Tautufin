"""Statistiques par utilisateur."""

from . import database


def list_users_with_stats() -> list[dict]:
    """Tous les utilisateurs Jellyfin connus + agrégats (vue admin)."""
    return database.query(
        """
        SELECT u.jellyfin_user_id, u.username, u.is_admin, u.is_active,
               u.last_activity,
               COUNT(h.id) AS total_plays,
               COALESCE(SUM(h.play_duration), 0) AS total_duration,
               MAX(h.started_at) AS last_played
        FROM users u
        LEFT JOIN session_history h ON h.jellyfin_user_id = u.jellyfin_user_id
        GROUP BY u.jellyfin_user_id
        ORDER BY total_plays DESC, u.username
        """
    )


def get_username(jellyfin_user_id: str) -> str | None:
    row = database.query_one(
        "SELECT username FROM users WHERE jellyfin_user_id = ?", (jellyfin_user_id,)
    )
    if row:
        return row["username"]
    row = database.query_one(
        "SELECT user_name AS username FROM session_history"
        " WHERE jellyfin_user_id = ? AND user_name IS NOT NULL LIMIT 1",
        (jellyfin_user_id,),
    )
    return row["username"] if row else None


def user_overview(jellyfin_user_id: str) -> dict:
    """Stats agrégées d'un utilisateur (page profil / détail admin)."""
    totals = database.query_one(
        """
        SELECT COUNT(*) AS total_plays,
               COALESCE(SUM(play_duration), 0) AS total_duration,
               MIN(started_at) AS first_played,
               MAX(started_at) AS last_played
        FROM session_history WHERE jellyfin_user_id = ?
        """,
        (jellyfin_user_id,),
    )
    by_type = database.query(
        """
        SELECT item_type, COUNT(*) AS plays,
               COALESCE(SUM(play_duration), 0) AS duration
        FROM session_history WHERE jellyfin_user_id = ?
        GROUP BY item_type ORDER BY plays DESC
        """,
        (jellyfin_user_id,),
    )
    top_clients = database.query(
        """
        SELECT COALESCE(client_name, 'Inconnu') AS client, COUNT(*) AS plays
        FROM session_history WHERE jellyfin_user_id = ?
        GROUP BY client ORDER BY plays DESC LIMIT 5
        """,
        (jellyfin_user_id,),
    )
    recent = database.query(
        """
        SELECT started_at, item_type, item_name, series_name, season_number,
               episode_number, play_duration, percent_complete, client_name
        FROM session_history WHERE jellyfin_user_id = ?
        ORDER BY started_at DESC LIMIT 10
        """,
        (jellyfin_user_id,),
    )
    return {
        "username": get_username(jellyfin_user_id),
        "jellyfin_user_id": jellyfin_user_id,
        "totals": totals,
        "by_type": by_type,
        "top_clients": top_clients,
        "recent": recent,
    }
