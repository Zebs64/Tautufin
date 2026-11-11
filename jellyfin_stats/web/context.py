"""Contexte partagé par les modules de routes : services (API Jellyfin,
moniteur d'activité, scheduler), dépendances d'authentification et helpers
de rendu. Construit une fois par application dans ``create_app``."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request

from .. import (
    __version__,
    auth,
    database,
    graphs,
    image_cache,
)
from .. import users as users_mod
from .. import wrapped as wrapped_mod
from ..activity import ActivityMonitor
from ..auth import CurrentUser, RateLimiter
from ..config import ACCENT_COLORS, Config
from ..jellyfin_api import JellyfinAPI
from ..scheduler import Scheduler
from .helpers import (
    BRANDING_EXTS,
    make_templates,
)


def make_context(config: Config) -> SimpleNamespace:
    api = JellyfinAPI(config)
    monitor = ActivityMonitor(config)
    scheduler = Scheduler(config, api, monitor)
    rate_limiter = RateLimiter()
    templates = make_templates()
    # Libellé d'un client dans les tableaux server-rendered : « Plex » pour les
    # lectures sans client (selon la config), sinon tiret.
    templates.env.filters["client_label"] = lambda name: (
        name or ("Plex" if config.unknown_as_plex else "—")
    )
    auth.init(config.secret_key, config.session_lifetime)

    def _user_profile(jellyfin_user_id: str) -> dict:
        """Profil utilisateur, avec relabel des clients inconnus en « Plex »
        si l'option est activée (la page utilisateur les conserve toujours)."""
        profile = users_mod.user_overview(jellyfin_user_id)
        # Carte « Wrapped » : dernière année ouverte + rendez-vous de l'année en cours.
        available = wrapped_mod.available_years(jellyfin_user_id)
        profile["wrapped_latest"] = available[0] if available else None
        upcoming = wrapped_mod.upcoming_year(jellyfin_user_id)
        profile["wrapped_upcoming"] = upcoming
        profile["wrapped_opening"] = wrapped_mod.opening_label(upcoming) if upcoming else None
        today = wrapped_mod._today()
        profile["calendar"] = users_mod.daily_calendar(
            jellyfin_user_id, today - timedelta(days=364), today
        )
        if config.unknown_as_plex:
            for row in profile.get("top_clients", []):
                if row["client"] == "Inconnu":
                    row["client"] = "Plex"
            for row in profile.get("recent", []):
                if not row.get("client_name"):
                    row["client_name"] = "Plex"
        return profile

    def _client_breakdown(days, metric, user_id, year=None) -> dict:
        """Graphe clients (accueil + graphs) avec masquage / relabel selon la
        config des clients inconnus."""
        return graphs.by_client(
            days,
            metric,
            user_id,
            year,
            hide_unknown=config.hide_unknown_clients,
            unknown_label="Plex" if config.unknown_as_plex else "Inconnu",
        )

    config_dir = Path(config.path).parent
    image_cache_dir = image_cache.cache_dir(config)
    branding_dir = config_dir / "branding"

    # --- Dépendances d'authentification -----------------------------------

    def get_user(request: Request) -> CurrentUser | None:
        return auth.resolve_session(request.cookies.get(auth.SESSION_COOKIE))

    def require_user(request: Request) -> CurrentUser:
        user = get_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentification requise")
        return user

    def require_admin(user: CurrentUser = Depends(require_user)) -> CurrentUser:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Réservé aux administrateurs")
        return user

    def require_global_view(user: CurrentUser = Depends(require_user)) -> CurrentUser:
        """Admin ou droit « vision » : consultation des stats de tous les
        utilisateurs (mais pas la configuration)."""
        if not user.can_view_everyone:
            raise HTTPException(
                status_code=403, detail="Réservé aux administrateurs et au droit « vision »"
            )
        return user

    def require_library_access(user: CurrentUser = Depends(require_user)) -> CurrentUser:
        if not user.is_admin and not config.allow_user_library_pages:
            raise HTTPException(
                status_code=403, detail="Pages bibliothèques désactivées par l'admin"
            )
        return user

    # --- Helpers ------------------------------------------------------------

    def _allow_library(user) -> bool:
        return bool(user) and (user.is_admin or config.allow_user_library_pages)

    def _branding_file(name: str) -> Path | None:
        if branding_dir.is_dir():
            for f in branding_dir.iterdir():
                if f.stem == name and f.suffix.lower() in BRANDING_EXTS:
                    return f
        return None

    def render(request, template, user=None, **ctx):
        ctx.setdefault("page", "")
        logo = _branding_file("logo")
        ctx.update(
            user=user,
            version=__version__,
            allow_library=_allow_library(user),
            jellyfin_configured=config.jellyfin_configured,
            logo_ver=int(logo.stat().st_mtime) if logo else 0,
            has_logo=logo is not None,
            accent=config.accent,
        )
        return templates.TemplateResponse(request, template, ctx)

    def is_configured() -> bool:
        return config.jellyfin_configured or auth.has_local_admin()

    def scoped_user_id(user: CurrentUser, requested: str | None) -> str | None:
        """Isolation des données : un non-admin ne voit que son UserId,
        imposé depuis la session serveur (jamais depuis le client)."""
        if user.can_view_everyone:
            return requested or None
        return user.jellyfin_user_id or "__none__"

    def _render_settings(request, user, **ctx):
        return render(
            request,
            "settings.html",
            user=user,
            page="settings",
            config=config,
            local_users=auth.list_local_users(),
            jf_users=database.query(
                "SELECT jellyfin_user_id, username FROM users ORDER BY username"
            ),
            jf_users_admin=users_mod.list_users_for_admin(),
            accent_colors=ACCENT_COLORS,
            **ctx,
        )

    return SimpleNamespace(
        config=config,
        api=api,
        monitor=monitor,
        scheduler=scheduler,
        rate_limiter=rate_limiter,
        templates=templates,
        _user_profile=_user_profile,
        _client_breakdown=_client_breakdown,
        config_dir=config_dir,
        image_cache_dir=image_cache_dir,
        branding_dir=branding_dir,
        get_user=get_user,
        require_user=require_user,
        require_admin=require_admin,
        require_global_view=require_global_view,
        require_library_access=require_library_access,
        _allow_library=_allow_library,
        _branding_file=_branding_file,
        render=render,
        is_configured=is_configured,
        scoped_user_id=scoped_user_id,
        _render_settings=_render_settings,
    )
