"""Requêtes sur l'historique de lecture (filtres, tri, pagination).

Isolation des données : ``user_id`` est fourni par l'appelant (main.py) qui
l'impose depuis la session serveur pour un non-admin — jamais depuis un
paramètre client.
"""

from . import database

# Tri whitelisté : clé exposée à l'API → colonne SQL.
SORT_COLUMNS = {
    "date": "started_at",
    "user": "user_name",
    "media": "item_name",
    "type": "item_type",
    "duration": "play_duration",
    "percent": "percent_complete",
    "client": "client_name",
    "ip": "ip_address",
}

PAGE_SIZE_MAX = 200


def get_history(
    user_id: str | None = None,
    media_type: str | None = None,
    library_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
    sort: str = "date",
    order: str = "desc",
    page: int = 1,
    page_size: int = 25,
) -> dict:
    where, params = ["1=1"], []
    if user_id:
        where.append("jellyfin_user_id = ?")
        params.append(user_id)
    if media_type:
        where.append("item_type = ?")
        params.append(media_type)
    if library_id:
        where.append("library_id = ?")
        params.append(library_id)
    if date_from:
        where.append("date(started_at) >= date(?)")
        params.append(date_from)
    if date_to:
        where.append("date(started_at) <= date(?)")
        params.append(date_to)
    if search:
        where.append("(item_name LIKE ? OR series_name LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where_sql = " AND ".join(where)
    sort_col = SORT_COLUMNS.get(sort, "started_at")
    direction = "ASC" if order.lower() == "asc" else "DESC"
    page = max(1, page)
    page_size = min(max(1, page_size), PAGE_SIZE_MAX)

    total = database.query_one(
        f"SELECT COUNT(*) AS n FROM session_history WHERE {where_sql}", params
    )["n"]
    rows = database.query(
        f"""
        SELECT id, started_at, stopped_at, jellyfin_user_id, user_name, item_id,
               item_type, item_name, series_name, season_number, episode_number,
               library_name, play_duration, runtime_seconds, percent_complete,
               client_name, device_name, ip_address, play_method,
               video_resolution, source
        FROM session_history
        WHERE {where_sql}
        ORDER BY {sort_col} {direction}, id {direction}
        LIMIT ? OFFSET ?
        """,
        params + [page_size, (page - 1) * page_size],
    )
    return {"total": total, "page": page, "page_size": page_size, "rows": rows}
