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


def _filters(
    user_id: str | None = None,
    media_type: str | None = None,
    library_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
) -> tuple[str, list]:
    """Clause WHERE commune à la liste paginée et à l'export CSV."""
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
    # Bornes appliquées à la colonne nue (et non à date(started_at)) pour que
    # SQLite utilise l'index sur started_at.
    if date_from:
        where.append("started_at >= date(?)")
        params.append(date_from)
    if date_to:
        where.append("started_at < date(?, '+1 day')")
        params.append(date_to)
    if search:
        where.append("(item_name LIKE ? OR series_name LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    return " AND ".join(where), params


def _order_by(sort: str, order: str) -> str:
    direction = "ASC" if order.lower() == "asc" else "DESC"
    return f"{SORT_COLUMNS.get(sort, 'started_at')} {direction}, id {direction}"


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
    where_sql, params = _filters(user_id, media_type, library_id, date_from, date_to, search)
    page = max(1, page)
    page_size = min(max(1, page_size), PAGE_SIZE_MAX)

    total = database.query_one(
        f"SELECT COUNT(*) AS n FROM session_history WHERE {where_sql}", params
    )["n"]
    rows = database.query(
        f"""
        SELECT sh.id, sh.started_at, sh.stopped_at, sh.jellyfin_user_id,
               sh.user_name, sh.item_id, sh.item_type, sh.item_name,
               sh.series_name, sh.season_number, sh.episode_number,
               sh.library_name, sh.play_duration, sh.runtime_seconds,
               sh.percent_complete, sh.client_name, sh.device_name,
               sh.ip_address, sh.play_method, sh.video_resolution, sh.source,
               -- Vignette : pour un épisode, le poster de la série plutôt que
               -- l'image de l'épisode (par identifiant, sinon par nom pour les
               -- lectures pas encore rattachées) ; repli sur le média.
               COALESCE(
                   sh.series_id,
                   (SELECT i.item_id FROM items i
                    WHERE i.type = 'Series' AND i.name = sh.series_name LIMIT 1),
                   sh.item_id) AS image_id
        FROM session_history sh
        WHERE {where_sql}
        ORDER BY {_order_by(sort, order)}
        LIMIT ? OFFSET ?
        """,
        params + [page_size, (page - 1) * page_size],
    )
    return {"total": total, "page": page, "page_size": page_size, "rows": rows}


EXPORT_COLUMNS = [
    "started_at",
    "stopped_at",
    "user_name",
    "item_type",
    "item_name",
    "series_name",
    "season_number",
    "episode_number",
    "library_name",
    "play_duration",
    "percent_complete",
    "client_name",
    "device_name",
    "play_method",
    "video_resolution",
    "source",
    "ip_address",
]


def export_rows(
    user_id: str | None = None,
    media_type: str | None = None,
    library_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
    sort: str = "date",
    order: str = "desc",
) -> list[dict]:
    """Toutes les lectures correspondant aux filtres, sans pagination."""
    where_sql, params = _filters(user_id, media_type, library_id, date_from, date_to, search)
    return database.query(
        f"SELECT {', '.join(EXPORT_COLUMNS)} FROM session_history"
        f" WHERE {where_sql} ORDER BY {_order_by(sort, order)}",
        params,
    )
