"""Pages HTML (accueil, historique, graphiques, profils, bibliothèques…)."""

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    RedirectResponse,
)

from .. import (
    clients,
    database,
    graphs,
    infer_history,
    media,
)
from .. import libraries as libraries_mod
from .. import users as users_mod
from .. import wrapped as wrapped_mod
from ..auth import CurrentUser
from .helpers import (
    JELLYFIN_ID_RE,
    MEDIA_TYPES,
)


def register(app: FastAPI, context) -> None:
    config = context.config
    _user_profile = context._user_profile
    require_user = context.require_user
    require_admin = context.require_admin
    require_global_view = context.require_global_view
    require_library_access = context.require_library_access
    render = context.render
    is_configured = context.is_configured
    scoped_user_id = context.scoped_user_id

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    @app.get("/")
    def home(request: Request):
        if not is_configured():
            return RedirectResponse("/setup", status_code=303)
        user = require_user(request)
        return render(request, "home.html", user=user, page="home")

    @app.get("/history")
    def history_page(request: Request, user: CurrentUser = Depends(require_user)):
        jf_users = users_mod.list_users_with_stats() if user.can_view_everyone else []
        libs = database.query("SELECT library_id, name FROM libraries ORDER BY name")
        return render(
            request,
            "history.html",
            user=user,
            page="history",
            jf_users=jf_users,
            libs=libs,
            media_types=MEDIA_TYPES,
        )

    @app.get("/media/{item_id}")
    def media_detail_page(
        request: Request, item_id: str, user: CurrentUser = Depends(require_user)
    ):
        if not JELLYFIN_ID_RE.match(item_id):
            raise HTTPException(status_code=404, detail="Média inconnu")
        # Isolation : un non-admin ne voit que ses propres visionnages.
        detail = media.media_overview(item_id, user_id=scoped_user_id(user, None))
        if not detail:
            raise HTTPException(status_code=404, detail="Média inconnu")
        if config.unknown_as_plex:
            for row in detail.get("recent", []):
                if not row.get("client_name"):
                    row["client_name"] = "Plex"
        return render(request, "media.html", user=user, page="history", media=detail)

    @app.get("/graphs")
    def graphs_page(request: Request, user: CurrentUser = Depends(require_user)):
        jf_users = users_mod.list_users_with_stats() if user.can_view_everyone else []
        return render(
            request,
            "graphs.html",
            user=user,
            page="graphs",
            jf_users=jf_users,
            years=graphs.available_years(),
        )

    @app.get("/clients")
    def clients_page(request: Request, user: CurrentUser = Depends(require_user)):
        jf_users = users_mod.list_users_with_stats() if user.can_view_everyone else []
        return render(
            request,
            "clients.html",
            user=user,
            page="clients",
            jf_users=jf_users,
            years=clients.available_years(scoped_user_id(user, None)),
        )

    @app.get("/users")
    def users_page(request: Request, user: CurrentUser = Depends(require_global_view)):
        return render(
            request, "users.html", user=user, page="users", users=users_mod.list_users_with_stats()
        )

    @app.get("/users/{jellyfin_user_id}")
    def user_detail_page(
        request: Request, jellyfin_user_id: str, user: CurrentUser = Depends(require_user)
    ):
        if not user.can_view_everyone and jellyfin_user_id != user.jellyfin_user_id:
            raise HTTPException(
                status_code=403, detail="Vous ne pouvez consulter que votre propre profil."
            )
        return render(
            request, "user.html", user=user, page="users", profile=_user_profile(jellyfin_user_id)
        )

    @app.get("/users/{jellyfin_user_id}/wrapped")
    def user_wrapped_page(
        request: Request,
        jellyfin_user_id: str,
        year: int | None = Query(None),
        user: CurrentUser = Depends(require_user),
    ):
        if not user.can_view_everyone and jellyfin_user_id != user.jellyfin_user_id:
            raise HTTPException(
                status_code=403, detail="Vous ne pouvez consulter que votre propre profil."
            )
        years = wrapped_mod.available_years(jellyfin_user_id)
        upcoming = wrapped_mod.upcoming_year(jellyfin_user_id)
        # Année demandée pas encore ouverte (ou rien d'autre à montrer) :
        # écran « rendez-vous » au lieu des chiffres.
        show_upcoming = upcoming is not None and (year == upcoming or not years)
        selected = (
            None if show_upcoming else (year if year in years else (years[0] if years else None))
        )
        data = wrapped_mod.build(jellyfin_user_id, selected) if selected else None
        if (
            data
            and config.unknown_as_plex
            and data.get("top_client")
            and not data["top_client"]["label"]
        ):
            data["top_client"]["label"] = "Plex"
        return render(
            request,
            "wrapped.html",
            user=user,
            page="users",
            jellyfin_user_id=jellyfin_user_id,
            username=users_mod.get_username(jellyfin_user_id),
            years=years,
            selected_year=selected,
            data=data,
            upcoming_year=upcoming,
            show_upcoming=show_upcoming,
            opening_label=wrapped_mod.opening_label(upcoming) if upcoming else None,
        )

    @app.get("/profile")
    def profile_page(request: Request, user: CurrentUser = Depends(require_user)):
        if not user.jellyfin_user_id:
            return render(request, "user.html", user=user, page="profile", profile=None)
        return render(
            request,
            "user.html",
            user=user,
            page="profile",
            profile=_user_profile(user.jellyfin_user_id),
        )

    @app.get("/libraries")
    def libraries_page(request: Request, user: CurrentUser = Depends(require_library_access)):
        return render(
            request,
            "libraries.html",
            user=user,
            page="libraries",
            libraries=libraries_mod.list_libraries_with_stats(),
        )

    @app.get("/libraries/{library_id}")
    def library_detail_page(
        request: Request, library_id: str, user: CurrentUser = Depends(require_library_access)
    ):
        library = libraries_mod.get_library(library_id)
        if not library:
            raise HTTPException(status_code=404, detail="Bibliothèque inconnue")
        return render(
            request,
            "library.html",
            user=user,
            page="libraries",
            library=library,
            detail=libraries_mod.library_detail(library_id),
        )

    @app.get("/import")
    def import_page(request: Request, user: CurrentUser = Depends(require_admin)):
        return render(
            request,
            "import.html",
            user=user,
            page="import",
            minimum_duration=config.minimum_duration,
            minimum_percent=config.minimum_percent,
            jf_users=database.query(
                "SELECT jellyfin_user_id, username FROM users ORDER BY username"
            ),
            inferred_count=infer_history.count_inferred(),
        )
