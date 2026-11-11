"""Requêtes sur l'historique de lecture (filtres, tri, pagination).

La liste affiche une ligne par visionnage (viewings.py) : filtres et date sur
son premier segment, durée = somme des segments, % vu = progression finale.
Ses segments s'obtiennent avec ``viewing_segments``. L'export CSV reste au
niveau des segments (données brutes).

Isolation des données : ``user_id`` est fourni par l'appelant (main.py) qui
l'impose depuis la session serveur pour un non-admin — jamais depuis un
paramètre client.
"""

from . import database, viewings
from .labels import media_type_label

# Infobulle du badge « estimée » (viewings.is_estimated), selon l'origine.
ESTIMATE_HINTS = {
    "infer": "Lecture estimée : reconstituée depuis le statut « Lu » de Jellyfin."
    " Date et durée sont approximatives.",
    "import": "Lecture estimée : reconstituée par Streamystats. Date et durée sont approximatives.",
}

# Tri whitelisté : clé exposée à l'API → colonne SQL (export, par segment).
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
# Même chose pour la liste par visionnage (requête ci-dessous).
_VIEWING_SORT = {
    **{key: f"sh.{column}" for key, column in SORT_COLUMNS.items()},
    "duration": "v.total_duration",
    "percent": "final_percent",
}

_ROW_COLUMNS = """
    sh.id, sh.started_at, sh.jellyfin_user_id, sh.user_name, sh.item_id,
    sh.item_type, sh.item_name, sh.series_name, sh.season_number,
    sh.episode_number, sh.library_name, sh.runtime_seconds, sh.client_name,
    sh.device_name, sh.ip_address, sh.play_method, sh.video_resolution,
    sh.source
"""

PAGE_SIZE_MAX = 200


def _filters(
    user_id: str | None = None,
    media_type: str | None = None,
    library_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
    alias: str = "",
    item_id: str | None = None,
    series_name: str | None = None,
) -> tuple[str, list]:
    """Clause WHERE commune à la liste paginée et à l'export CSV."""
    p = f"{alias}." if alias else ""
    where, params = ["1=1"], []
    if user_id:
        where.append(f"{p}jellyfin_user_id = ?")
        params.append(user_id)
    if media_type:
        where.append(f"{p}item_type = ?")
        params.append(media_type)
    if library_id:
        where.append(f"{p}library_id = ?")
        params.append(library_id)
    # Bornes appliquées à la colonne nue (et non à date(started_at)) pour que
    # SQLite utilise l'index sur started_at.
    if date_from:
        where.append(f"{p}started_at >= date(?)")
        params.append(date_from)
    if date_to:
        where.append(f"{p}started_at < date(?, '+1 day')")
        params.append(date_to)
    if search:
        where.append(f"({p}item_name LIKE ? OR {p}series_name LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    # Fiche média (pas exposés à l'API de l'historique).
    if item_id:
        where.append(f"{p}item_id = ?")
        params.append(item_id)
    if series_name:
        where.append(f"{p}item_type = 'Episode' AND {p}series_name = ?")
        params.append(series_name)
    return " AND ".join(where), params


def _order_by(sort: str, order: str, columns: dict = SORT_COLUMNS, id_column="id") -> str:
    direction = "ASC" if order.lower() == "asc" else "DESC"
    return f"{columns.get(sort, columns['date'])} {direction}, {id_column} {direction}"


def _image_id(alias: str) -> str:
    """Vignette : pour un épisode, le poster de la série plutôt que l'image de
    l'épisode (par identifiant, sinon par nom pour les lectures pas encore
    rattachées) ; repli sur le média."""
    return f"""COALESCE(
        {alias}.series_id,
        (SELECT i.item_id FROM items i
         WHERE i.type = 'Series' AND i.name = {alias}.series_name LIMIT 1),
        {alias}.item_id)"""


def _viewings_cte(where_sql: str) -> str:
    """CTE ``v`` : un visionnage par premier segment (alias ``f``) satisfaisant
    ``where_sql``, avec ses agrégats et son dernier segment (``last_id``).
    « MAX(...), s.id » : SQLite renvoie l'id de la ligne qui porte le maximum.
    À joindre à ``session_history sh`` (premier segment) et ``l`` (dernier)."""
    return f"""
        WITH v AS (
            SELECT f.id AS vid, COUNT(s.id) AS segments,
                   COALESCE(SUM(s.play_duration), f.play_duration) AS total_duration,
                   MAX(s.started_at) AS last_started_at, s.id AS last_id
            FROM session_history f
            LEFT JOIN session_history s ON s.viewing_id = f.id
            WHERE {database.first_segment_sql("f")} AND {where_sql}
            GROUP BY f.id
        )"""


# Progression finale d'un visionnage : celle du segment seul, sinon la position
# live du dernier segment, sinon la part cumulée vue (plafonnée à 100 %).
_FINAL_PERCENT = """CASE
    WHEN v.segments <= 1 THEN sh.percent_complete
    WHEN l.source = 'live' AND l.percent_complete IS NOT NULL THEN l.percent_complete
    WHEN l.runtime_seconds > 0 THEN MIN(100.0, v.total_duration * 100.0 / l.runtime_seconds)
    ELSE l.percent_complete
END"""


def average_final_percent(where_sql: str, params: list) -> float | None:
    """Moyenne des progressions finales des visionnages filtrés (``where_sql``
    porte sur le premier segment, alias ``f``)."""
    row = database.query_one(
        f"""
        {_viewings_cte(where_sql)}
        SELECT AVG({_FINAL_PERCENT}) AS avg_percent
        FROM v
        JOIN session_history sh ON sh.id = v.vid
        LEFT JOIN session_history l ON l.id = v.last_id
        """,
        params,
    )
    return row["avg_percent"] if row else None


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
    item_id: str | None = None,
    series_name: str | None = None,
) -> dict:
    where_sql, params = _filters(
        user_id,
        media_type,
        library_id,
        date_from,
        date_to,
        search,
        alias="f",
        item_id=item_id,
        series_name=series_name,
    )
    page = max(1, page)
    page_size = min(max(1, page_size), PAGE_SIZE_MAX)
    first = database.first_segment_sql("f")
    total = database.query_one(
        f"SELECT COUNT(*) AS n FROM session_history f WHERE {first} AND {where_sql}", params
    )["n"]
    rows = database.query(
        f"""
        {_viewings_cte(where_sql)}
        SELECT {_ROW_COLUMNS},
               MAX(v.segments, 1) AS segments,
               v.total_duration AS play_duration,
               COALESCE(l.stopped_at, sh.stopped_at) AS stopped_at,
               {_FINAL_PERCENT} AS final_percent,
               {viewings.estimated_sql("sh")} AS estimated,
               {_image_id("sh")} AS image_id
        FROM v
        JOIN session_history sh ON sh.id = v.vid
        LEFT JOIN session_history l ON l.id = v.last_id
        ORDER BY {_order_by(sort, order, _VIEWING_SORT, "sh.id")}
        LIMIT ? OFFSET ?
        """,
        params + [page_size, (page - 1) * page_size],
    )
    for row in rows:
        row["percent_complete"] = row.pop("final_percent")
        row["estimated"] = bool(row["estimated"])
        row["estimate_hint"] = ESTIMATE_HINTS.get(row["source"]) if row["estimated"] else None
    for row in rows:
        row["item_type_label"] = media_type_label(row["item_type"])
    return {"total": total, "page": page, "page_size": page_size, "rows": rows}


def last_play(user_id: str | None = None) -> dict | None:
    """Dernière lecture terminée (état vide de l'accueil). Vue globale : sans
    les utilisateurs masqués, comme les statistiques."""
    where, params = "1=1", []
    if user_id:
        where, params = "jellyfin_user_id = ?", [user_id]
    else:
        where = "jellyfin_user_id NOT IN (SELECT jellyfin_user_id FROM users WHERE hidden = 1)"
    return database.query_one(
        f"""
        SELECT item_id, item_type, item_name, series_name, season_number, episode_number,
               user_name,
               COALESCE(NULLIF(stopped_at, ''),
                        datetime(started_at, '+' || COALESCE(play_duration, 0) || ' seconds'))
                   AS ended_at
        FROM session_history WHERE {where}
        ORDER BY started_at DESC, id DESC LIMIT 1
        """,
        params,
    )


def viewing_segments(viewing_id: int, user_id: str | None = None) -> list[dict]:
    """Segments d'un visionnage, dans l'ordre. ``user_id`` : même isolation
    que la liste (un non-admin ne voit que ses propres lectures)."""
    where, params = "viewing_id = ?", [viewing_id]
    if user_id:
        where += " AND jellyfin_user_id = ?"
        params.append(user_id)
    return database.query(
        f"""
        SELECT id, started_at, stopped_at, play_duration, percent_complete, source,
               client_name, device_name, ip_address, play_method
        FROM session_history WHERE {where}
        ORDER BY started_at, id
        """,
        params,
    )


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
