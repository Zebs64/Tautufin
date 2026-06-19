"""Statistiques par média (page détail d'un film / épisode / série).

Combien de fois un média a été vu, par qui, quand — avec une timeline mensuelle.
Scopable sur un utilisateur (un non-admin ne voit que ses propres visionnages,
cohérent avec l'isolation de la page historique)."""

import json
from datetime import date, timedelta

from . import database


def _meta(item_id: str) -> dict | None:
    """Métadonnées du média depuis le cache items ; repli sur l'historique si
    le média a disparu de Jellyfin (toujours présent dans session_history)."""
    meta = database.query_one(
        """
        SELECT i.item_id, i.name, i.type, i.series_name, i.season_number,
               i.episode_number, i.year, i.genres, i.runtime_seconds,
               i.video_resolution, i.video_codec, i.audio_codec,
               i.library_id, l.name AS library_name
        FROM items i LEFT JOIN libraries l ON l.library_id = i.library_id
        WHERE i.item_id = ?
        """,
        (item_id,),
    )
    if meta:
        return meta
    return database.query_one(
        """
        SELECT item_id, item_name AS name, item_type AS type, series_name,
               season_number, episode_number, NULL AS year, genres,
               runtime_seconds, video_resolution, video_codec, audio_codec,
               library_id, library_name
        FROM session_history WHERE item_id = ? ORDER BY started_at DESC LIMIT 1
        """,
        (item_id,),
    )


def _month_range(first_month: str, last_month: str) -> list[str]:
    """Énumère les mois 'YYYY-MM' de first à last inclus (axe sans trous)."""
    y, m = int(first_month[:4]), int(first_month[5:7])
    ey, em = int(last_month[:4]), int(last_month[5:7])
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
        if len(out) > 600:  # garde-fou (50 ans)
            break
    return out


# Granularité de la timeline selon l'étendue : SQLite strftime, libellé court,
# et fonction de remplissage de l'axe (clé brute → toutes les clés de la plage).
_FRENCH_MONTHS = ["", "janv.", "févr.", "mars", "avr.", "mai", "juin",
                  "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def _timeline(where: str, params: list) -> dict:
    """Timeline des lectures à granularité adaptative (jour / mois / année)
    pour rester lisible quelle que soit l'étendue de l'historique du média."""
    span = database.query_one(
        f"""
        SELECT MIN(started_at) AS mn, MAX(started_at) AS mx,
               julianday(MAX(started_at)) - julianday(MIN(started_at)) AS days
        FROM session_history WHERE {where} AND started_at IS NOT NULL
        """,
        params,
    )
    if not span or not span["mn"]:
        return {"categories": [], "labels": [], "data": [], "granularity": "none"}

    days = span["days"] or 0
    if days <= 92:
        gran, fmt = "day", "%Y-%m-%d"
    elif days <= 1000:
        gran, fmt = "month", "%Y-%m"
    else:
        gran, fmt = "year", "%Y"

    rows = database.query(
        f"""
        SELECT strftime('{fmt}', started_at) AS k, COUNT(*) AS plays
        FROM session_history WHERE {where} AND started_at IS NOT NULL
        GROUP BY k ORDER BY k
        """,
        params,
    )
    counts = {r["k"]: r["plays"] for r in rows}
    first_k, last_k = rows[0]["k"], rows[-1]["k"]

    if gran == "day":
        d0, d1 = date.fromisoformat(first_k), date.fromisoformat(last_k)
        cats, d = [], d0
        while d <= d1:
            cats.append(d.isoformat())
            d += timedelta(days=1)
        labels = [f"{c[8:10]}/{c[5:7]}" for c in cats]
    elif gran == "month":
        cats = _month_range(first_k, last_k)
        labels = [f"{_FRENCH_MONTHS[int(c[5:7])]} {c[:4]}" for c in cats]
    else:
        cats = [str(y) for y in range(int(first_k), int(last_k) + 1)]
        labels = cats[:]

    return {"categories": cats, "labels": labels,
            "data": [counts.get(c, 0) for c in cats], "granularity": gran}


def media_overview(item_id: str, user_id: str | None = None) -> dict | None:
    """Vue détaillée d'un média. ``user_id`` restreint aux lectures de cet
    utilisateur (None = tous, vue admin)."""
    meta = _meta(item_id)
    if not meta:
        return None

    if meta["type"] == "Series":
        # L'historique Jellyfin est stocké sur les épisodes. Une fiche /media
        # ciblant l'item Series doit donc agréger les lectures des épisodes de
        # cette série, sinon les tops d'accueil ouvrent une page vide.
        where, params = "item_type = 'Episode' AND series_name = ?", [meta["name"]]
    else:
        where, params = "item_id = ?", [item_id]
    if user_id:
        where += " AND jellyfin_user_id = ?"
        params.append(user_id)

    totals = database.query_one(
        f"""
        SELECT COUNT(*) AS total_plays,
               COUNT(DISTINCT jellyfin_user_id) AS viewers,
               COALESCE(SUM(play_duration), 0) AS total_duration,
               MIN(started_at) AS first_played,
               MAX(started_at) AS last_played
        FROM session_history WHERE {where}
        """,
        params,
    )
    by_user = database.query(
        f"""
        SELECT jellyfin_user_id, user_name, COUNT(*) AS plays,
               COALESCE(SUM(play_duration), 0) AS duration,
               MAX(started_at) AS last_played
        FROM session_history WHERE {where}
        GROUP BY jellyfin_user_id ORDER BY plays DESC, duration DESC
        """,
        params,
    )
    recent = database.query(
        f"""
        SELECT started_at, jellyfin_user_id, user_name, play_duration,
               percent_complete, client_name, device_name
        FROM session_history WHERE {where}
        ORDER BY started_at DESC LIMIT 25
        """,
        params,
    )
    timeline = _timeline(where, params)

    try:
        genres = json.loads(meta["genres"]) if meta.get("genres") else []
    except (ValueError, TypeError):
        genres = []

    return {
        "meta": meta,
        "genres": genres,
        "totals": totals,
        "by_user": by_user,
        "recent": recent,
        "timeline": timeline,
    }
