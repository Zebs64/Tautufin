"""Statistiques par utilisateur."""

from . import database


def list_users_with_stats() -> list[dict]:
    """Utilisateurs Jellyfin + agrégats pour la page Utilisateurs. Les
    utilisateurs masqués sont exclus (invisibles partout)."""
    return database.query(
        """
        SELECT u.jellyfin_user_id, u.username, u.is_admin, u.is_active,
               u.last_activity,
               COUNT(h.id) AS total_plays,
               COALESCE(SUM(h.play_duration), 0) AS total_duration,
               MAX(h.started_at) AS last_played
        FROM users u
        LEFT JOIN session_history h ON h.jellyfin_user_id = u.jellyfin_user_id
        WHERE u.hidden = 0
        GROUP BY u.jellyfin_user_id
        ORDER BY total_plays DESC, u.username
        """
    )


def list_users_for_admin() -> list[dict]:
    """Tous les utilisateurs Jellyfin (masqués inclus) avec leurs droits, pour
    le bloc de gestion des réglages."""
    return database.query(
        """
        SELECT u.jellyfin_user_id, u.username, u.is_admin, u.is_active,
               u.hidden, u.access_blocked, u.can_view_all,
               COUNT(h.id) AS total_plays, MAX(h.started_at) AS last_played
        FROM users u
        LEFT JOIN session_history h ON h.jellyfin_user_id = u.jellyfin_user_id
        GROUP BY u.jellyfin_user_id
        ORDER BY u.is_admin DESC, total_plays DESC, u.username
        """
    )


def set_user_flags(jellyfin_user_id: str, hidden: bool,
                   access_blocked: bool, can_view_all: bool) -> None:
    database.execute(
        "UPDATE users SET hidden = ?, access_blocked = ?, can_view_all = ?"
        " WHERE jellyfin_user_id = ?",
        (int(hidden), int(access_blocked), int(can_view_all), jellyfin_user_id),
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
        SELECT COALESCE(NULLIF(client_name, ''), 'Inconnu') AS client, COUNT(*) AS plays
        FROM session_history WHERE jellyfin_user_id = ?
        GROUP BY client ORDER BY plays DESC LIMIT 5
        """,
        (jellyfin_user_id,),
    )
    recent = database.query(
        """
        SELECT sh.started_at, sh.item_id, sh.item_type, sh.item_name,
               sh.series_name, sh.season_number, sh.episode_number,
               sh.play_duration, sh.percent_complete, sh.client_name,
               sh.device_name,
               COALESCE(
                   (SELECT i.item_id FROM items i
                    WHERE i.type = 'Series' AND i.name = sh.series_name LIMIT 1),
                   sh.item_id) AS image_id
        FROM session_history sh WHERE sh.jellyfin_user_id = ?
        ORDER BY sh.started_at DESC LIMIT 10
        """,
        (jellyfin_user_id,),
    )
    return {
        "username": get_username(jellyfin_user_id),
        "jellyfin_user_id": jellyfin_user_id,
        "totals": totals,
        "by_type": by_type,
        "top_clients": top_clients,
        "top_per_kind": _top_per_kind(jellyfin_user_id),
        "recent": recent,
    }


def _top_per_kind(jellyfin_user_id: str) -> list[dict]:
    """Film / Série / Chanson le plus visionné par cet utilisateur (un par
    catégorie), classé par temps de visionnage cumulé. ``item_id`` cible la
    page média ; pour une série, l'identifiant de la série porte aussi le
    poster."""
    film = database.query_one(
        """
        SELECT item_name AS label, COALESCE(SUM(play_duration), 0) AS duration,
               MAX(item_id) AS item_id
        FROM session_history WHERE jellyfin_user_id = ? AND item_type = 'Movie'
        GROUP BY item_name ORDER BY duration DESC LIMIT 1
        """,
        (jellyfin_user_id,),
    )
    serie = database.query_one(
        """
        SELECT sh.series_name AS label, COALESCE(SUM(sh.play_duration), 0) AS duration,
               (SELECT i.item_id FROM items i
                WHERE i.type = 'Series' AND i.name = sh.series_name LIMIT 1) AS item_id
        FROM session_history sh
        WHERE sh.jellyfin_user_id = ? AND sh.item_type = 'Episode'
          AND sh.series_name IS NOT NULL
        GROUP BY sh.series_name ORDER BY duration DESC LIMIT 1
        """,
        (jellyfin_user_id,),
    )
    chanson = database.query_one(
        """
        SELECT item_name AS label, COALESCE(SUM(play_duration), 0) AS duration,
               MAX(item_id) AS item_id
        FROM session_history
        WHERE jellyfin_user_id = ? AND item_type IN ('Audio', 'MusicVideo')
        GROUP BY item_name ORDER BY duration DESC LIMIT 1
        """,
        (jellyfin_user_id,),
    )
    out = []
    for kind, icon, row in (("Film", "🎬", film),
                            ("Série", "📺", serie),
                            ("Chanson", "🎵", chanson)):
        out.append({"kind": kind, "icon": icon,
                    "label": row["label"] if row else None,
                    "duration": row["duration"] if row else 0,
                    "item_id": row["item_id"] if row else None})
    return out
