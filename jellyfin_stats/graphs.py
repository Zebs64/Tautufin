"""Données des graphiques (format Chart.js, inspiré de plexpy/graphs.py).

Toutes les fonctions retournent ``{"categories": [...], "series":
[{"name": ..., "data": [...]}]}`` et acceptent :
- ``days``    : profondeur de l'historique (None ou 0 = depuis toujours),
- ``year``    : année civile — prioritaire sur ``days`` si renseignée,
- ``metric``  : 'plays' (nombre de lectures) ou 'duration' (secondes vues),
- ``user_id`` : filtre utilisateur — imposé par main.py depuis la session
  pour un non-admin (isolation des données).
"""

from datetime import date, timedelta

from . import database

WEEKDAYS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

# Catégorisation des types Jellyfin pour les comparaisons films/séries/musique.
TYPE_CASE = """
    CASE
        WHEN item_type = 'Movie' THEN 'Films'
        WHEN item_type = 'Episode' THEN 'Séries'
        WHEN item_type IN ('Audio', 'MusicVideo', 'AudioBook') THEN 'Musique'
        ELSE 'Autre'
    END
"""

GROUP_FORMATS = {"day": "%Y-%m-%d", "week": "%Y-S%W", "month": "%Y-%m"}


def _metric_sql(metric: str) -> str:
    return "SUM(play_duration)" if metric == "duration" else "COUNT(*)"


def _base_where(days, user_id: str | None, year: int | None = None) -> tuple[str, list]:
    where, params = ["1=1"], []
    if year:
        where.append("strftime('%Y', started_at) = ?")
        params.append(f"{int(year):04d}")
    elif days:
        where.append("started_at >= datetime('now', 'localtime', ?)")
        params.append(f"-{int(days)} days")
    # days falsy et pas d'année : aucune borne → « depuis toujours »
    if user_id:
        where.append("jellyfin_user_id = ?")
        params.append(user_id)
    else:
        # Vue globale : les utilisateurs « masqués » sont totalement exclus
        # (totaux compris). Leur propre profil reste visible (user_id imposé).
        where.append("jellyfin_user_id NOT IN "
                     "(SELECT jellyfin_user_id FROM users WHERE hidden = 1)")
    return " AND ".join(where), params


def _time_axis(days, group: str, year: int | None = None) -> list[str]:
    """Axe temporel complet (les périodes sans lecture restent à zéro)."""
    fmt = GROUP_FORMATS[group]
    if year:
        start = date(year, 1, 1)
        end = min(date(year, 12, 31), date.today())
        if start > end:  # année future
            end = start
    else:
        end = date.today()
        start = end - timedelta(days=days)
    labels, seen = [], set()
    day = start
    while day <= end:
        label = day.strftime(fmt)
        if label not in seen:
            seen.add(label)
            labels.append(label)
        day += timedelta(days=1)
    return labels


def plays_over_time(days=30, group: str = "day", metric: str = "plays",
                    user_id: str | None = None, year: int | None = None) -> dict:
    """Lectures (ou durée) par jour/semaine/mois, ventilées films/séries/musique."""
    group = group if group in GROUP_FORMATS else "day"
    if not days and not year:
        days = 30  # l'axe temporel ne peut pas être infini
    where, params = _base_where(days, user_id, year)
    rows = database.query(
        f"""
        SELECT strftime('{GROUP_FORMATS[group]}', started_at) AS bucket,
               {TYPE_CASE} AS category, {_metric_sql(metric)} AS value
        FROM session_history WHERE {where}
        GROUP BY bucket, category
        """,
        params,
    )
    labels = _time_axis(days, group, year)
    index = {label: i for i, label in enumerate(labels)}
    by_category: dict[str, list] = {}
    totals = [0] * len(labels)
    for row in rows:
        if row["bucket"] not in index:
            continue
        data = by_category.setdefault(row["category"], [0] * len(labels))
        data[index[row["bucket"]]] += row["value"] or 0
        totals[index[row["bucket"]]] += row["value"] or 0
    series = [{"name": name, "data": data}
              for name, data in by_category.items()
              if name != "Autre" or any(data)]
    series.append({"name": "Total", "data": totals})
    return {"categories": labels, "series": series}


def _grouped(days, metric: str, user_id: str | None, group_expr: str,
             label: str, limit: int = 10, extra_where: str = "",
             year: int | None = None) -> dict:
    where, params = _base_where(days, user_id, year)
    rows = database.query(
        f"""
        SELECT {group_expr} AS label, {_metric_sql(metric)} AS value
        FROM session_history
        WHERE {where} {extra_where}
        GROUP BY {group_expr}
        ORDER BY value DESC
        LIMIT {int(limit)}
        """,
        params,
    )
    return {
        "categories": [r["label"] or "Inconnu" for r in rows],
        "series": [{"name": label, "data": [r["value"] or 0 for r in rows]}],
    }


def by_user(days=30, metric: str = "plays", year: int | None = None) -> dict:
    """Répartition par utilisateur (admin uniquement, vérifié par main.py)."""
    return _grouped(days, metric, None, "user_name", "Par utilisateur", year=year)


def by_library(days=30, metric: str = "plays", user_id=None, year=None) -> dict:
    return _grouped(days, metric, user_id,
                    "COALESCE(library_name, 'Inconnue')", "Par bibliothèque",
                    year=year)


def by_resolution(days=30, metric: str = "plays", user_id=None, year=None) -> dict:
    return _grouped(days, metric, user_id,
                    "COALESCE(video_resolution, 'Inconnue')", "Par résolution",
                    year=year)


def by_client(days=30, metric: str = "plays", user_id=None, year=None,
              hide_unknown: bool = True, unknown_label: str = "Inconnu") -> dict:
    # Beaucoup de lectures importées (ou inférées en amont par Jellyfin /
    # Streamystats) n'ont pas de client connu — souvent un historique Plex
    # synchronisé. Selon la config on les masque de ce graphe (ils restent
    # comptés ailleurs) ou on les affiche sous un libellé dédié (« Plex »…).
    extra = "AND client_name IS NOT NULL AND client_name != ''" if hide_unknown else ""
    data = _grouped(days, metric, user_id, "client_name", "Par client",
                    extra_where=extra, year=year)
    if not hide_unknown:
        # _grouped mappe les clients vides/NULL sur « Inconnu » : on les
        # renomme selon la préférence (« Plex » le cas échéant).
        data["categories"] = [unknown_label if c == "Inconnu" else c
                              for c in data["categories"]]
    return data


def by_play_method(days=30, metric: str = "plays", user_id=None, year=None) -> dict:
    return _grouped(days, metric, user_id,
                    "COALESCE(play_method, 'Inconnu')", "Par méthode de lecture",
                    year=year)


def transcode_seconds(days=30, user_id=None, year=None) -> int:
    """Secondes de lecture avec transcodage *vidéo* sur la période. On exclut
    les méthodes « v:direct » (vidéo en direct, seul l'audio est transcodé) qui
    ne coûtent quasiment rien en CPU/énergie. Sert à l'estimation de conso."""
    where, params = _base_where(days, user_id, year)
    row = database.query_one(
        f"""
        SELECT COALESCE(SUM(play_duration), 0) AS seconds
        FROM session_history
        WHERE {where} AND play_method LIKE 'Transcode%'
          AND play_method NOT LIKE '%v:direct%'
        """,
        params,
    )
    return row["seconds"] if row else 0


def by_day_of_week(days=30, metric: str = "plays", user_id=None, year=None) -> dict:
    """Activité par jour de la semaine (axe fixe Lundi → Dimanche)."""
    where, params = _base_where(days, user_id, year)
    rows = database.query(
        f"""
        SELECT CAST(strftime('%w', started_at) AS INTEGER) AS dow,
               {_metric_sql(metric)} AS value
        FROM session_history WHERE {where}
        GROUP BY dow
        """,
        params,
    )
    data = [0] * 7
    for r in rows:
        # strftime('%w') : 0 = dimanche → remappé sur 0 = lundi
        data[(r["dow"] + 6) % 7] = r["value"] or 0
    return {"categories": WEEKDAYS_FR,
            "series": [{"name": "Par jour de la semaine", "data": data}]}


def by_hour_of_day(days=30, metric: str = "plays", user_id=None, year=None) -> dict:
    """Activité par heure de la journée (axe fixe 00h → 23h)."""
    where, params = _base_where(days, user_id, year)
    rows = database.query(
        f"""
        SELECT CAST(strftime('%H', started_at) AS INTEGER) AS hour,
               {_metric_sql(metric)} AS value
        FROM session_history WHERE {where}
        GROUP BY hour
        """,
        params,
    )
    data = [0] * 24
    for r in rows:
        data[r["hour"]] = r["value"] or 0
    return {"categories": [f"{h:02d}h" for h in range(24)],
            "series": [{"name": "Par heure de la journée", "data": data}]}


def by_genre(days=30, metric: str = "plays", user_id=None, year=None) -> dict:
    where, params = _base_where(days, user_id, year)
    rows = database.query(
        f"""
        SELECT je.value AS label, {_metric_sql(metric)} AS value
        FROM session_history, json_each(session_history.genres) AS je
        WHERE {where} AND genres IS NOT NULL
        GROUP BY je.value
        ORDER BY value DESC
        LIMIT 10
        """,
        params,
    )
    return {
        "categories": [r["label"] for r in rows],
        "series": [{"name": "Par genre", "data": [r["value"] or 0 for r in rows]}],
    }


def top_people(days=30, kind: str = "actor", metric: str = "plays",
               user_id=None, year=None, limit: int = 10) -> dict:
    """Top acteurs / réalisateurs / scénaristes selon les lectures (jointure
    historique × distribution stockée dans items.people)."""
    person_type = {"actor": "Actor", "director": "Director",
                   "writer": "Writer"}.get(kind, "Actor")
    label = {"actor": "Acteurs", "director": "Réalisateurs",
             "writer": "Scénaristes"}.get(kind, "Personnes")
    where, params = _base_where(days, user_id, year)
    rows = database.query(
        f"""
        SELECT json_extract(p.value, '$.Name') AS label, {_metric_sql(metric)} AS value
        FROM session_history h
        JOIN items i ON i.item_id = h.item_id
        JOIN json_each(i.people) AS p
        WHERE {where} AND i.people IS NOT NULL
          AND json_extract(p.value, '$.Type') = ?
        GROUP BY label ORDER BY value DESC LIMIT {int(limit)}
        """,
        params + [person_type],
    )
    return {
        "categories": [r["label"] for r in rows],
        "series": [{"name": label, "data": [r["value"] or 0 for r in rows]}],
    }


def top_media(days=30, kind: str = "movie", metric: str = "plays",
              user_id=None, limit: int = 10, year=None) -> list[dict]:
    """Top films/séries avec identifiant d'image pour les vignettes du
    dashboard. ``image_id`` pointe vers le média dont le poster est servi par
    le proxy /image/item/ (la série elle-même pour un top séries)."""
    where, params = _base_where(days, user_id, year)
    if kind == "series":
        # NB : ``items`` a aussi une colonne ``series_name`` ; on qualifie donc
        # la colonne externe (sh.series_name) sinon la corrélation se lie à la
        # table interne (toujours NULL pour une Series) et le match échoue.
        rows = database.query(
            f"""
            SELECT sh.series_name AS label, {_metric_sql(metric)} AS value,
                   (SELECT i.item_id FROM items i
                    WHERE i.type = 'Series' AND i.name = sh.series_name LIMIT 1
                   ) AS image_id,
                   MAX(sh.item_id) AS fallback_id
            FROM session_history sh
            WHERE {where} AND sh.item_type = 'Episode' AND sh.series_name IS NOT NULL
            GROUP BY sh.series_name ORDER BY value DESC LIMIT {int(limit)}
            """,
            params,
        )
    else:
        rows = database.query(
            f"""
            SELECT item_name AS label, {_metric_sql(metric)} AS value,
                   MAX(item_id) AS image_id, NULL AS fallback_id
            FROM session_history
            WHERE {where} AND item_type = 'Movie'
            GROUP BY item_name ORDER BY value DESC LIMIT {int(limit)}
            """,
            params,
        )
    for r in rows:
        r["image_id"] = r["image_id"] or r.pop("fallback_id", None)
        r.pop("fallback_id", None)
    return rows


def popular_media(days=30, kind: str = "movie", user_id=None,
                  limit: int = 10, year=None) -> list[dict]:
    """Médias les plus *populaires* = nombre d'utilisateurs distincts les ayant
    regardés (logique « popular » de Tautulli, indépendante de plays/durée)."""
    where, params = _base_where(days, user_id, year)
    if kind == "series":
        rows = database.query(
            f"""
            SELECT sh.series_name AS label,
                   COUNT(DISTINCT sh.jellyfin_user_id) AS value,
                   (SELECT i.item_id FROM items i
                    WHERE i.type = 'Series' AND i.name = sh.series_name LIMIT 1
                   ) AS image_id,
                   MAX(sh.item_id) AS fallback_id
            FROM session_history sh
            WHERE {where} AND sh.item_type = 'Episode' AND sh.series_name IS NOT NULL
            GROUP BY sh.series_name ORDER BY value DESC, label LIMIT {int(limit)}
            """,
            params,
        )
    else:
        rows = database.query(
            f"""
            SELECT item_name AS label,
                   COUNT(DISTINCT jellyfin_user_id) AS value,
                   MAX(item_id) AS image_id, NULL AS fallback_id
            FROM session_history
            WHERE {where} AND item_type = 'Movie'
            GROUP BY item_name ORDER BY value DESC, label LIMIT {int(limit)}
            """,
            params,
        )
    for r in rows:
        r["image_id"] = r["image_id"] or r.pop("fallback_id", None)
        r.pop("fallback_id", None)
    return rows


def recently_watched(days=None, user_id=None, limit: int = 10) -> list[dict]:
    """Derniers médias regardés (films + épisodes), dédupliqués par média sur
    la dernière lecture. ``last_watch`` = horodatage, ``user_name`` = dernier
    spectateur. Pour un épisode, ``image_id`` pointe sur la *série* (même
    logique que les films : poster + fanart de la série, pas de l'épisode)."""
    where, params = _base_where(days, user_id)
    rows = database.query(
        f"""
        SELECT item_type, item_name, series_name, season_number, episode_number,
               item_id, started_at AS last_watch, user_name
        FROM session_history
        WHERE {where} AND item_type IN ('Movie', 'Episode')
        ORDER BY started_at DESC LIMIT 300
        """,
        params,
    )
    out, seen, series_ids = [], set(), {}
    for r in rows:
        is_episode = r["item_type"] == "Episode" and r["series_name"]
        if is_episode:
            label = (f"{r['series_name']} · S{r['season_number'] or 0}"
                     f"E{r['episode_number'] or 0}")
        else:
            label = r["item_name"]
        if not label or label in seen:
            continue
        seen.add(label)
        out.append({
            "label": label, "last_watch": r["last_watch"],
            "user_name": r["user_name"], "image_id": r["item_id"],
            "_series": r["series_name"] if is_episode else None,
        })
        if len(out) >= limit:
            break
    # Résout le poster de la série pour les épisodes (un seul lookup par série).
    for o in out:
        series = o.pop("_series")
        if not series:
            continue
        if series not in series_ids:
            row = database.query_one(
                "SELECT item_id FROM items WHERE type = 'Series' AND name = ?"
                " LIMIT 1", (series,))
            series_ids[series] = row["item_id"] if row else None
        if series_ids[series]:
            o["image_id"] = series_ids[series]
    return out


def top_libraries(days=30, metric: str = "plays", user_id=None,
                  limit: int = 10, year=None) -> list[dict]:
    """Bibliothèques les plus actives. ``collection_type`` (movies/tvshows/
    music…) sert à choisir une icône côté front (comme les clients)."""
    where, params = _base_where(days, user_id, year)
    return database.query(
        f"""
        SELECT COALESCE(sh.library_name, 'Inconnue') AS label,
               {_metric_sql(metric)} AS value,
               (SELECT l.collection_type FROM libraries l
                WHERE l.name = sh.library_name LIMIT 1) AS collection_type
        FROM session_history sh
        WHERE {where} AND sh.library_name IS NOT NULL
        GROUP BY label ORDER BY value DESC LIMIT {int(limit)}
        """,
        params,
    )


def top_users(days=30, metric: str = "plays", limit: int = 10) -> list[dict]:
    """Top utilisateurs avec leur UserId pour l'avatar (vue admin)."""
    where, params = _base_where(days, None)
    return database.query(
        f"""
        SELECT MAX(user_name) AS label, jellyfin_user_id AS user_id,
               {_metric_sql(metric)} AS value
        FROM session_history WHERE {where}
        GROUP BY jellyfin_user_id ORDER BY value DESC LIMIT {int(limit)}
        """,
        params,
    )


def top_items(days=30, kind: str = "movie", metric: str = "plays",
              user_id=None, limit: int = 10, year=None) -> dict:
    if kind == "series":
        return _grouped(days, metric, user_id, "series_name", "Top séries", limit,
                        "AND item_type = 'Episode' AND series_name IS NOT NULL",
                        year=year)
    if kind == "episode":
        expr = ("series_name || ' — S' || COALESCE(season_number, 0) || 'E' || "
                "COALESCE(episode_number, 0) || ' ' || item_name")
        return _grouped(days, metric, user_id, expr, "Top épisodes", limit,
                        "AND item_type = 'Episode'", year=year)
    return _grouped(days, metric, user_id, "item_name", "Top films", limit,
                    "AND item_type = 'Movie'", year=year)


def available_years() -> list[int]:
    """Années civiles présentes dans l'historique (pour le sélecteur)."""
    rows = database.query(
        "SELECT DISTINCT strftime('%Y', started_at) AS y FROM session_history"
        " WHERE started_at IS NOT NULL ORDER BY y DESC"
    )
    return [int(r["y"]) for r in rows if r["y"]]
