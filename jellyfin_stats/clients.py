"""Diagnostic historique des clients fondé uniquement sur session_history."""

from . import database


_METHOD = "LOWER(REPLACE(COALESCE(play_method, ''), ' ', ''))"


def _where(days, user_id: str | None, year: int | None = None) -> tuple[str, list]:
    where, params = ["1=1"], []
    if year:
        where.append("strftime('%Y', started_at) = ?")
        params.append(f"{int(year):04d}")
    elif days:
        where.append("started_at >= datetime('now', 'localtime', ?)")
        params.append(f"-{int(days)} days")
    if user_id:
        where.append("jellyfin_user_id = ?")
        params.append(user_id)
    else:
        where.append(
            "jellyfin_user_id NOT IN "
            "(SELECT jellyfin_user_id FROM users WHERE hidden = 1)"
        )
    return " AND ".join(where), params


def available_years(user_id: str | None = None) -> list[int]:
    where, params = _where(None, user_id)
    rows = database.query(
        f"""
        SELECT DISTINCT strftime('%Y', started_at) AS year
        FROM session_history WHERE {where} AND started_at IS NOT NULL
        ORDER BY year DESC
        """,
        params,
    )
    return [int(row["year"]) for row in rows if row["year"]]


def _distribution(where: str, params: list, column: str,
                  unknown: str = "Inconnu", limit: int = 10) -> dict:
    rows = database.query(
        f"""
        SELECT COALESCE(NULLIF(TRIM({column}), ''), ?) AS label,
               COUNT(*) AS value
        FROM session_history WHERE {where}
        GROUP BY label ORDER BY value DESC, label ASC LIMIT {int(limit)}
        """,
        [unknown, *params],
    )
    return {
        "categories": [row["label"] for row in rows],
        "series": [{"name": "Lectures", "data": [row["value"] for row in rows]}],
    }


def build(days: int | None = 30, user_id: str | None = None, year: int | None = None,
          *, hide_unknown: bool = True, unknown_label: str = "Inconnu",
          watts: int = 0, electricity_price: float = 0.0) -> dict:
    """Construit les KPIs, le classement, cinq graphiques et les constats.

    ``user_id`` est déjà imposé depuis la session par ``main.py``. Sans filtre,
    les utilisateurs marqués comme masqués sont exclus comme dans ``graphs.py``.
    """
    where, params = _where(days, user_id, year)
    summary_row = database.query_one(
        f"""
        SELECT COUNT(*) AS plays,
               COALESCE(SUM(CASE WHEN play_duration > 0 THEN play_duration ELSE 0 END), 0)
                   AS duration_seconds,
               SUM(CASE WHEN {_METHOD} = 'directplay' THEN 1 ELSE 0 END) AS direct_play,
               SUM(CASE WHEN {_METHOD} = 'directstream' THEN 1 ELSE 0 END) AS direct_stream,
               SUM(CASE WHEN {_METHOD} LIKE 'transcode%' THEN 1 ELSE 0 END) AS transcode,
               SUM(CASE WHEN {_METHOD} NOT IN ('directplay', 'directstream')
                              AND {_METHOD} NOT LIKE 'transcode%'
                        THEN 1 ELSE 0 END) AS unknown_method,
               COALESCE(SUM(CASE WHEN {_METHOD} LIKE 'transcode%'
                                      AND {_METHOD} NOT LIKE '%v:direct%'
                                 THEN CASE WHEN play_duration > 0 THEN play_duration ELSE 0 END
                                 ELSE 0 END), 0) AS transcode_seconds
        FROM session_history WHERE {where}
        """,
        params,
    ) or {}
    plays = int(summary_row.get("plays") or 0)
    method_counts = {
        "direct_play": int(summary_row.get("direct_play") or 0),
        "direct_stream": int(summary_row.get("direct_stream") or 0),
        "transcode": int(summary_row.get("transcode") or 0),
        "unknown": int(summary_row.get("unknown_method") or 0),
    }

    client_filter = ""
    if hide_unknown:
        client_filter = " AND client_name IS NOT NULL AND TRIM(client_name) != ''"
    client_rows = database.query(
        f"""
        SELECT COALESCE(NULLIF(TRIM(client_name), ''), ?) AS client,
               COUNT(*) AS plays,
               COALESCE(SUM(CASE WHEN play_duration > 0 THEN play_duration ELSE 0 END), 0)
                   AS duration_seconds,
               SUM(CASE WHEN {_METHOD} = 'directplay' THEN 1 ELSE 0 END) AS direct_play,
               SUM(CASE WHEN {_METHOD} = 'directstream' THEN 1 ELSE 0 END) AS direct_stream,
               SUM(CASE WHEN {_METHOD} LIKE 'transcode%' THEN 1 ELSE 0 END) AS transcode,
               SUM(CASE WHEN {_METHOD} NOT IN ('directplay', 'directstream')
                              AND {_METHOD} NOT LIKE 'transcode%'
                        THEN 1 ELSE 0 END) AS unknown,
               MAX(started_at) AS last_used
        FROM session_history WHERE {where}{client_filter}
        GROUP BY client
        ORDER BY plays DESC, duration_seconds DESC, client ASC
        LIMIT 10
        """,
        [unknown_label, *params],
    )
    active_row = database.query_one(
        f"""
        SELECT COUNT(DISTINCT COALESCE(NULLIF(TRIM(client_name), ''), ?)) AS total
        FROM session_history WHERE {where}{client_filter}
        """,
        [unknown_label, *params],
    )
    for row in client_rows:
        for key in ("plays", "duration_seconds", "direct_play", "direct_stream",
                    "transcode", "unknown"):
            row[key] = int(row[key] or 0)

    transcode_seconds = int(summary_row.get("transcode_seconds") or 0)
    transcode_kwh = max(0, watts) / 1000 * transcode_seconds / 3600
    summary = {
        "active_clients": int(active_row["total"] or 0) if active_row else 0,
        "plays": plays,
        "duration_seconds": int(summary_row.get("duration_seconds") or 0),
        "direct_play_percent": round(method_counts["direct_play"] / plays * 100, 1)
        if plays else 0.0,
        "direct_stream_percent": round(method_counts["direct_stream"] / plays * 100, 1)
        if plays else 0.0,
        "transcode_percent": round(method_counts["transcode"] / plays * 100, 1)
        if plays else 0.0,
        "transcode_seconds": transcode_seconds,
        "transcode_kwh": round(transcode_kwh, 2),
        "transcode_cost": round(transcode_kwh * max(0.0, electricity_price), 2),
    }

    resolutions = _distribution(where, params, "video_resolution", "Inconnue")
    video_codecs = _distribution(where, params, "video_codec", "Inconnu")
    audio_codecs = _distribution(where, params, "audio_codec", "Inconnu")
    diagnostics = []
    transcoders = [row for row in client_rows if row["transcode"] > 0]
    if transcoders:
        leader = max(transcoders, key=lambda row: (row["transcode"], row["client"]))
        share = round(leader["transcode"] / method_counts["transcode"] * 100)
        diagnostics.append({
            "kind": "transcode_leader",
            "text": f"{leader['client']} concentre {share} % des lectures transcodées.",
        })
    if plays >= 5 and summary["direct_play_percent"] >= 90:
        diagnostics.append({
            "kind": "direct_play",
            "text": f"Excellent taux de Direct Play : {summary['direct_play_percent']:.1f} %.",
        })
    four_k = database.query_one(
        f"""
        SELECT COUNT(*) AS plays FROM session_history
        WHERE {where} AND {_METHOD} LIKE 'transcode%'
          AND (LOWER(COALESCE(video_resolution, '')) LIKE '%2160%'
               OR LOWER(COALESCE(video_resolution, '')) LIKE '%4k%')
        """,
        params,
    )
    if four_k and four_k["plays"]:
        diagnostics.append({
            "kind": "transcode_4k",
            "text": f"{four_k['plays']} lecture(s) 4K ont été transcodées sur la période.",
        })

    categories = [row["client"] for row in client_rows]
    return {
        "summary": summary,
        "methods": method_counts,
        "clients": client_rows,
        "charts": {
            "usage": {
                "categories": categories,
                "series": [{"name": "Lectures", "data": [row["plays"] for row in client_rows]}],
            },
            "methods_by_client": {
                "categories": categories,
                "series": [
                    {"name": "Direct Play", "data": [row["direct_play"] for row in client_rows]},
                    {"name": "Direct Stream", "data": [row["direct_stream"] for row in client_rows]},
                    {"name": "Transcodage", "data": [row["transcode"] for row in client_rows]},
                    {"name": "Inconnu", "data": [row["unknown"] for row in client_rows]},
                ],
            },
            "resolutions": resolutions,
            "video_codecs": video_codecs,
            "audio_codecs": audio_codecs,
        },
        "diagnostics": diagnostics,
    }
