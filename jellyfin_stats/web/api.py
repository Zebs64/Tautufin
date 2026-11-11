"""API JSON consommée par le front (activité, historique, graphiques, synchro)."""

import csv
import io
import time

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import (
    StreamingResponse,
)

from .. import (
    clients,
    graphs,
    history,
)
from .. import users as users_mod
from ..auth import CurrentUser
from ..jellyfin_api import JellyfinError
from ..scheduler import get_sync_status, start_sync
from .helpers import (
    PERIOD_DAYS,
    _csv_cell,
)


def register(app: FastAPI, context) -> None:
    config = context.config
    api = context.api
    monitor = context.monitor
    _client_breakdown = context._client_breakdown
    require_user = context.require_user
    require_admin = context.require_admin
    require_global_view = context.require_global_view
    scoped_user_id = context.scoped_user_id

    # ------------------------------------------------------------------
    # API JSON
    # ------------------------------------------------------------------

    @app.get("/api/activity")
    def api_activity(user: CurrentUser = Depends(require_user)):
        sessions = monitor.snapshot(scoped_user_id(user, None))
        for s in sessions:
            if not user.is_admin:  # détails réseau réservés à l'admin
                s.pop("ip_address", None)
                s.pop("is_lan", None)
            if not user.can_view_everyone:  # identité réservée admin/vision
                s.pop("jellyfin_user_id", None)
        return {"sessions": sessions}

    @app.get("/api/history")
    def api_history(
        user: CurrentUser = Depends(require_user),
        user_id: str | None = Query(None),
        media_type: str | None = Query(None),
        library_id: str | None = Query(None),
        date_from: str | None = Query(None),
        date_to: str | None = Query(None),
        search: str | None = Query(None),
        sort: str = Query("date"),
        order: str = Query("desc"),
        page: int = Query(1, ge=1),
        page_size: int = Query(25, ge=1, le=200),
    ):
        result = history.get_history(
            user_id=scoped_user_id(user, user_id),
            media_type=media_type,
            library_id=library_id,
            date_from=date_from,
            date_to=date_to,
            search=search,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )
        if config.unknown_as_plex:
            for row in result["rows"]:
                if not row.get("client_name"):
                    row["client_name"] = "Plex"
        if not user.is_admin:
            for row in result["rows"]:
                row.pop("ip_address", None)
        return result

    @app.get("/api/history.csv")
    def api_history_csv(
        user: CurrentUser = Depends(require_user),
        user_id: str | None = Query(None),
        media_type: str | None = Query(None),
        library_id: str | None = Query(None),
        date_from: str | None = Query(None),
        date_to: str | None = Query(None),
        search: str | None = Query(None),
        sort: str = Query("date"),
        order: str = Query("desc"),
    ):
        """Export CSV de l'historique filtré (mêmes filtres et mêmes droits que
        /api/history, sans pagination). Point-virgule et BOM UTF-8 pour Excel."""
        rows = history.export_rows(
            user_id=scoped_user_id(user, user_id),
            media_type=media_type,
            library_id=library_id,
            date_from=date_from,
            date_to=date_to,
            search=search,
            sort=sort,
            order=order,
        )
        columns = [c for c in history.EXPORT_COLUMNS if c != "ip_address" or user.is_admin]
        if config.unknown_as_plex:
            for row in rows:
                if not row.get("client_name"):
                    row["client_name"] = "Plex"

        def lines():
            buffer = io.StringIO()
            writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
            yield "\ufeff"
            writer.writerow(columns)
            for row in rows:
                writer.writerow([_csv_cell(row[c]) for c in columns])
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate()
            yield buffer.getvalue()

        filename = f"tautufin-historique-{time.strftime('%Y%m%d')}.csv"
        return StreamingResponse(
            lines(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/home_stats")
    def api_home_stats(
        user: CurrentUser = Depends(require_user),
        period: str = Query("7d"),
        metric: str = Query("plays"),
    ):
        days = PERIOD_DAYS.get(period, 7)  # None = depuis toujours
        metric = metric if metric in ("plays", "duration") else "plays"
        uid = scoped_user_id(user, None)

        def flat(data):
            series = data["series"][0]["data"] if data["series"] else []
            return [
                {"label": c, "value": v} for c, v in zip(data["categories"], series, strict=False)
            ]

        stats = {
            "metric": metric,
            "top_movies": graphs.top_media(days, "movie", metric, uid, limit=10),
            "top_series": graphs.top_media(days, "series", metric, uid, limit=10),
            "recently_watched": graphs.recently_watched(days, uid, limit=10),
            "top_libraries": graphs.top_libraries(days, metric, uid, limit=10),
            "top_clients": flat(_client_breakdown(days, metric, uid)),
        }
        # « Populaires » = nb d'utilisateurs distincts : pertinent uniquement en
        # vue globale (un non-admin n'a que ses propres données → toujours 1).
        if user.can_view_everyone:
            stats["popular_movies"] = graphs.popular_media(days, "movie", uid, limit=10)
            stats["popular_series"] = graphs.popular_media(days, "series", uid, limit=10)
            stats["top_users"] = graphs.top_users(days, metric, limit=10)
        return stats

    # days=0 → None : agrégat sur tout l'historique ; year prioritaire.
    DaysParam = Query(30, ge=0, le=36500)
    YearParam = Query(None, ge=2000, le=2100)

    @app.get("/api/graphs/plays_over_time")
    def api_plays_over_time(
        user: CurrentUser = Depends(require_user),
        days: int = DaysParam,
        group: str = Query("day"),
        metric: str = Query("plays"),
        user_id: str | None = Query(None),
        year: int | None = YearParam,
    ):
        return graphs.plays_over_time(
            days or None, group, metric, scoped_user_id(user, user_id), year
        )

    @app.get("/api/clients")
    def api_clients(
        user: CurrentUser = Depends(require_user),
        days: int = DaysParam,
        user_id: str | None = Query(None),
        year: int | None = YearParam,
    ):
        return clients.build(
            days or None,
            scoped_user_id(user, user_id),
            year,
            hide_unknown=config.hide_unknown_clients,
            unknown_label="Plex" if config.unknown_as_plex else "Inconnu",
            watts=config.transcode_watts,
            electricity_price=config.electricity_price,
        )

    GRAPH_FUNCS = {
        "by_library": graphs.by_library,
        "by_genre": graphs.by_genre,
        "by_resolution": graphs.by_resolution,
        "by_play_method": graphs.by_play_method,
        "by_day_of_week": graphs.by_day_of_week,
        "by_hour_of_day": graphs.by_hour_of_day,
    }

    @app.get("/api/graphs/by_user")
    def api_graph_by_user(
        user: CurrentUser = Depends(require_global_view),
        days: int = DaysParam,
        metric: str = Query("plays"),
        year: int | None = YearParam,
    ):
        return graphs.by_user(days or None, metric, year)

    @app.get("/api/graphs/top_items")
    def api_graph_top_items(
        user: CurrentUser = Depends(require_user),
        days: int = DaysParam,
        kind: str = Query("movie"),
        metric: str = Query("plays"),
        user_id: str | None = Query(None),
        year: int | None = YearParam,
    ):
        return graphs.top_items(
            days or None, kind, metric, scoped_user_id(user, user_id), year=year
        )

    @app.get("/api/graphs/top_people")
    def api_graph_top_people(
        user: CurrentUser = Depends(require_user),
        days: int = DaysParam,
        kind: str = Query("actor"),
        metric: str = Query("plays"),
        user_id: str | None = Query(None),
        year: int | None = YearParam,
    ):
        return graphs.top_people(
            days or None, kind, metric, scoped_user_id(user, user_id), year=year
        )

    @app.get("/api/graphs/by_client")
    def api_graph_by_client(
        user: CurrentUser = Depends(require_user),
        days: int = DaysParam,
        metric: str = Query("plays"),
        user_id: str | None = Query(None),
        year: int | None = YearParam,
    ):
        return _client_breakdown(days or None, metric, scoped_user_id(user, user_id), year)

    @app.get("/api/graphs/transcode_cost")
    def api_transcode_cost(
        user: CurrentUser = Depends(require_user),
        days: int = DaysParam,
        user_id: str | None = Query(None),
        year: int | None = YearParam,
    ):
        """Estimation de la conso électrique imputable au transcodage vidéo sur
        la période/le filtre courant (hypothèses : surconsommation fixe et prix
        du kWh, cf. constantes ci-dessous — volontairement approximatif)."""
        seconds = graphs.transcode_seconds(days or None, scoped_user_id(user, user_id), year)
        hours = seconds / 3600
        watts = config.transcode_watts
        price = config.electricity_price
        kwh = watts / 1000 * hours
        return {
            "seconds": seconds,
            "hours": round(hours, 1),
            "kwh": round(kwh, 2),
            "cost": round(kwh * price, 2),
            "watts": watts,
            "eur_per_kwh": price,
        }

    @app.get("/api/graphs/{graph_name}")
    def api_graph(
        graph_name: str,
        user: CurrentUser = Depends(require_user),
        days: int = DaysParam,
        metric: str = Query("plays"),
        user_id: str | None = Query(None),
        year: int | None = YearParam,
    ):
        func = GRAPH_FUNCS.get(graph_name)
        if func is None:
            raise HTTPException(status_code=404, detail="Graphique inconnu")
        return func(days or None, metric, scoped_user_id(user, user_id), year)

    @app.get("/api/users")
    def api_users(user: CurrentUser = Depends(require_global_view)):
        return {"users": users_mod.list_users_with_stats()}

    @app.post("/api/settings/test-jellyfin")
    def api_test_jellyfin(payload: dict, user: CurrentUser = Depends(require_admin)):
        try:
            info = api.ping(
                base_url=(payload.get("url") or "").strip().rstrip("/"),
                api_key=(payload.get("api_key") or "").strip() or config.jellyfin_api_key,
            )
            return {"ok": True, "server": info.get("ServerName"), "version": info.get("Version")}
        except JellyfinError as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/sync")
    def api_sync(user: CurrentUser = Depends(require_admin)):
        if not config.jellyfin_configured:
            raise HTTPException(status_code=400, detail="Jellyfin non configuré")
        # Lance la synchro en arrière-plan (thread daemon) : elle survit à la
        # navigation. La progression se suit via GET /api/sync/status.
        started = start_sync(api)
        return {"started": started, **get_sync_status()}

    @app.get("/api/sync/status")
    def api_sync_status(user: CurrentUser = Depends(require_admin)):
        return get_sync_status()
