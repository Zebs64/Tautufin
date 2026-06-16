"""Rétrospective annuelle par utilisateur (« Wrapped », inspiré de Streamystats).

Agrège, pour un utilisateur Jellyfin et une année civile, l'ensemble des
statistiques affichées sur la page : volumes, tops films/séries/genres,
saisonnalité (mois / jour / heure), plus grosse journée, plus longue série de
jours consécutifs et profil de visionnage. Tout est dérivé de
``session_history`` (durées en secondes)."""

from datetime import date, timedelta

from . import database

WEEKDAYS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
MONTHS_FR = ["Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet",
             "Août", "Septembre", "Octobre", "Novembre", "Décembre"]


def user_years(jellyfin_user_id: str) -> list[int]:
    """Années civiles présentes dans l'historique de cet utilisateur."""
    rows = database.query(
        "SELECT DISTINCT strftime('%Y', started_at) AS y FROM session_history"
        " WHERE jellyfin_user_id = ? AND started_at IS NOT NULL ORDER BY y DESC",
        (jellyfin_user_id,))
    return [int(r["y"]) for r in rows if r["y"]]


def _longest_streak(days: list[str]) -> int:
    """Plus longue série de jours calendaires consécutifs (dates 'YYYY-MM-DD')."""
    if not days:
        return 0
    ordered = sorted(date.fromisoformat(d) for d in days)
    best = run = 1
    for prev, cur in zip(ordered, ordered[1:]):
        if cur - prev == timedelta(days=1):
            run += 1
            best = max(best, run)
        elif cur != prev:
            run = 1
    return best


def build(jellyfin_user_id: str, year: int) -> dict:
    """Toutes les sections du Wrapped pour (utilisateur, année)."""
    uid, y = jellyfin_user_id, f"{int(year):04d}"
    base = "jellyfin_user_id = ? AND strftime('%Y', started_at) = ?"
    p = (uid, y)

    totals = database.query_one(
        f"""
        SELECT COUNT(*) AS plays,
               COALESCE(SUM(play_duration), 0) AS duration,
               COUNT(DISTINCT CASE WHEN item_type = 'Movie' THEN item_name END) AS movies,
               SUM(CASE WHEN item_type = 'Episode' THEN 1 ELSE 0 END) AS episodes,
               COUNT(DISTINCT CASE WHEN item_type = 'Episode' THEN series_name END) AS series,
               COUNT(DISTINCT date(started_at)) AS active_days,
               AVG(percent_complete) AS avg_percent
        FROM session_history WHERE {base}
        """, p)

    if not totals or not totals["plays"]:
        return {"year": year, "empty": True, "totals": totals or {}}

    top_movies = database.query(
        f"""
        SELECT item_name AS label, COUNT(*) AS plays,
               COALESCE(SUM(play_duration), 0) AS duration, MAX(item_id) AS item_id
        FROM session_history WHERE {base} AND item_type = 'Movie'
        GROUP BY item_name ORDER BY duration DESC LIMIT 5
        """, p)

    top_series = database.query(
        f"""
        SELECT sh.series_name AS label, COUNT(*) AS plays,
               COALESCE(SUM(sh.play_duration), 0) AS duration,
               (SELECT i.item_id FROM items i
                WHERE i.type = 'Series' AND i.name = sh.series_name LIMIT 1) AS item_id
        FROM session_history sh
        WHERE sh.jellyfin_user_id = ? AND strftime('%Y', sh.started_at) = ?
          AND sh.item_type = 'Episode' AND sh.series_name IS NOT NULL
        GROUP BY sh.series_name ORDER BY duration DESC LIMIT 5
        """, p)

    top_genres = database.query(
        f"""
        SELECT je.value AS label, COUNT(*) AS plays
        FROM session_history, json_each(session_history.genres) AS je
        WHERE {base} AND genres IS NOT NULL
        GROUP BY je.value ORDER BY plays DESC LIMIT 8
        """, p)

    # Saisonnalité : mois, jour de semaine, heure (axes complets).
    months = [0] * 12
    for r in database.query(
            f"SELECT CAST(strftime('%m', started_at) AS INT) AS m,"
            f" COALESCE(SUM(play_duration), 0) AS d FROM session_history"
            f" WHERE {base} GROUP BY m", p):
        months[r["m"] - 1] = r["d"]

    dow = [0] * 7
    for r in database.query(
            f"SELECT CAST(strftime('%w', started_at) AS INT) AS w,"
            f" COALESCE(SUM(play_duration), 0) AS d FROM session_history"
            f" WHERE {base} GROUP BY w", p):
        dow[(r["w"] + 6) % 7] = r["d"]  # %w: 0 = dimanche → 0 = lundi

    hours = [0] * 24
    for r in database.query(
            f"SELECT CAST(strftime('%H', started_at) AS INT) AS h,"
            f" COALESCE(SUM(play_duration), 0) AS d FROM session_history"
            f" WHERE {base} GROUP BY h", p):
        hours[r["h"]] = r["d"]

    busiest = database.query_one(
        f"""
        SELECT date(started_at) AS day, COALESCE(SUM(play_duration), 0) AS duration,
               COUNT(*) AS plays
        FROM session_history WHERE {base}
        GROUP BY day ORDER BY duration DESC LIMIT 1
        """, p)

    top_client = database.query_one(
        f"""
        SELECT client_name AS label, COUNT(*) AS plays
        FROM session_history
        WHERE {base} AND client_name IS NOT NULL AND client_name != ''
        GROUP BY client_name ORDER BY plays DESC LIMIT 1
        """, p)

    first_play = database.query_one(
        f"""
        SELECT started_at, item_type, item_name, series_name
        FROM session_history WHERE {base}
        ORDER BY started_at LIMIT 1
        """, p)

    all_days = [r["day"] for r in database.query(
        f"SELECT DISTINCT date(started_at) AS day FROM session_history"
        f" WHERE {base} ORDER BY day", p)]

    peak_month = max(range(12), key=lambda i: months[i])
    fav_dow = max(range(7), key=lambda i: dow[i])
    fav_hour = max(range(24), key=lambda i: hours[i])

    # Comparaison avec l'année précédente (volume et lectures).
    prev = database.query_one(
        "SELECT COUNT(*) AS plays, COALESCE(SUM(play_duration), 0) AS duration"
        " FROM session_history WHERE jellyfin_user_id = ?"
        " AND strftime('%Y', started_at) = ?", (uid, f"{year - 1:04d}"))
    comparison = None
    if prev and prev["plays"]:
        def _pct(cur, old):
            return round((cur - old) / old * 100) if old else None
        comparison = {
            "prev_year": year - 1,
            "prev_duration": prev["duration"],
            "prev_plays": prev["plays"],
            "duration_pct": _pct(totals["duration"], prev["duration"]),
            "plays_pct": _pct(totals["plays"], prev["plays"]),
        }

    avg_percent = (round(totals["avg_percent"])
                   if totals["avg_percent"] is not None else None)

    return {
        "year": year,
        "empty": False,
        "totals": totals,
        "avg_percent": avg_percent,
        "comparison": comparison,
        "days_equiv": round(totals["duration"] / 86400, 1),
        "top_movies": top_movies,
        "top_series": top_series,
        "top_genres": top_genres,
        "months": months,
        "months_labels": MONTHS_FR,
        "peak_month": {"label": MONTHS_FR[peak_month], "duration": months[peak_month]},
        "fav_dow": {"label": WEEKDAYS_FR[fav_dow], "duration": dow[fav_dow]},
        "fav_hour": {"hour": fav_hour, "duration": hours[fav_hour]},
        "night_owl": fav_hour >= 22 or fav_hour < 5,
        "busiest_day": busiest,
        "top_client": top_client,
        "first_play": first_play,
        "longest_streak": _longest_streak(all_days),
    }
