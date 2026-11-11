"""Réglages (admin) : connexion Jellyfin, accès, comptes, maintenance."""

import logging

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import (
    RedirectResponse,
    Response,
)

from .. import (
    auth,
    database,
    image_cache,
)
from .. import users as users_mod
from ..auth import CurrentUser
from ..proxy import normalize_trusted_proxies

logger = logging.getLogger(__name__)


def register(app: FastAPI, context) -> None:
    config = context.config
    _render_settings = context._render_settings
    image_cache_dir = context.image_cache_dir
    require_admin = context.require_admin

    # ------------------------------------------------------------------
    # Settings (admin)
    # ------------------------------------------------------------------

    @app.get("/settings")
    def settings_page(
        request: Request, saved: int = 0, reset: int = 0, user: CurrentUser = Depends(require_admin)
    ):
        return _render_settings(request, user, saved=bool(saved), reset=bool(reset))

    @app.post("/settings/connection")
    def settings_connection_submit(
        request: Request,
        user: CurrentUser = Depends(require_admin),
        jellyfin_url: str = Form(""),
        api_key: str = Form(""),
        minimum_duration: int = Form(300),
        minimum_percent: int = Form(0),
        poll_interval: int = Form(15),
        sync_interval: int = Form(3600),
        session_grace: int = Form(90),
        unknown_as_plex: str = Form(None),
        hide_unknown_clients: str = Form(None),
        transcode_watts: int = Form(50),
        electricity_price: float = Form(0.27),
    ):
        """Bloc « Connexion Jellyfin » : serveur, capture/polling, clients."""
        config.set("Jellyfin", "url", jellyfin_url.strip().rstrip("/"))
        if api_key.strip():  # champ vide = clé inchangée
            config.set("Jellyfin", "api_key", api_key.strip())
        config.set("Monitoring", "minimum_duration", max(0, minimum_duration))
        config.set("Monitoring", "minimum_percent", min(100, max(0, minimum_percent)))
        config.set("Monitoring", "poll_interval", max(5, poll_interval))
        config.set("Monitoring", "sync_interval", max(60, sync_interval))
        config.set("Monitoring", "session_grace", max(0, session_grace))
        config.set("Clients", "unknown_as_plex", bool(unknown_as_plex))
        config.set("Clients", "hide_unknown_clients", bool(hide_unknown_clients))
        config.set("Energy", "transcode_watts", max(0, transcode_watts))
        config.set("Energy", "electricity_price", max(0.0, electricity_price))
        config.save()
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/access")
    def settings_access_submit(
        request: Request,
        user: CurrentUser = Depends(require_admin),
        session_lifetime: int = Form(604800),
        jellyfin_auth_enabled: str = Form(None),
        local_auth_enabled: str = Form(None),
        allow_user_library_pages: str = Form(None),
        trusted_proxies: str = Form(""),
    ):
        """Bloc « Accès » : authentification, sessions, droits d'interface,
        reverse proxy."""
        try:
            trusted_proxies = normalize_trusted_proxies(trusted_proxies)
        except ValueError as exc:
            return _render_settings(request, user, access_error=str(exc))
        config.set("Web", "trusted_proxies", trusted_proxies)
        config.set("Auth", "session_lifetime", max(300, session_lifetime))
        config.set("Auth", "jellyfin_auth_enabled", bool(jellyfin_auth_enabled))
        config.set("Auth", "local_auth_enabled", bool(local_auth_enabled))
        config.set("UI", "allow_user_library_pages", bool(allow_user_library_pages))
        config.save()
        auth.init(config.secret_key, config.session_lifetime)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/jellyfin-users")
    async def jellyfin_users_submit(request: Request, user: CurrentUser = Depends(require_admin)):
        """Met à jour les droits par utilisateur Jellyfin (masquage, blocage,
        vision). Le formulaire envoie, par utilisateur coché, les champs
        ``hidden_<id>`` / ``blocked_<id>`` / ``view_<id>``."""
        form = await request.form()
        for u in users_mod.list_users_for_admin():
            uid = u["jellyfin_user_id"]
            # Garde-fou : on ne peut pas se bloquer soi-même (anti-lockout).
            blocked = (f"blocked_{uid}" in form) and uid != user.jellyfin_user_id
            users_mod.set_user_flags(
                uid,
                hidden=f"hidden_{uid}" in form,
                access_blocked=blocked,
                can_view_all=f"view_{uid}" in form,
            )
            if blocked:
                # Coupe immédiatement les sessions Jellyfin actives du bloqué.
                database.execute(
                    "DELETE FROM http_sessions WHERE jellyfin_user_id = ?"
                    " AND auth_mode = 'jellyfin'",
                    (uid,),
                )
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/local-users")
    def create_local_user_route(
        request: Request,
        user: CurrentUser = Depends(require_admin),
        username: str = Form(...),
        password: str = Form(...),
        role: str = Form("user"),
        jellyfin_user_id: str = Form(""),
    ):
        username = username.strip()
        if not username or not password:
            return _render_settings(request, user, user_error="Nom et mot de passe obligatoires.")
        if auth.password_error(password):
            return _render_settings(request, user, user_error=auth.password_error(password))
        if auth.get_local_user(username):
            return _render_settings(
                request, user, user_error=f"Le compte « {username} » existe déjà."
            )
        auth.create_local_user(
            username,
            password,
            role if role in ("admin", "user") else "user",
            jellyfin_user_id.strip() or None,
        )
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/local-users/{local_id}/delete")
    def delete_local_user_route(
        request: Request, local_id: int, user: CurrentUser = Depends(require_admin)
    ):
        try:
            auth.delete_local_user(local_id)
        except auth.LastAdminError:
            return _render_settings(
                request,
                user,
                user_error="Impossible de supprimer le dernier compte admin local.",
            )
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/local-users/{local_id}/password")
    def reset_local_password_route(
        request: Request,
        local_id: int,
        user: CurrentUser = Depends(require_admin),
        new_password: str = Form(...),
    ):
        if auth.password_error(new_password):
            return _render_settings(request, user, user_error=auth.password_error(new_password))
        row = database.query_one("SELECT username FROM local_users WHERE id = ?", (local_id,))
        if row and new_password:
            auth.set_local_password(row["username"], new_password)
        return RedirectResponse("/settings", status_code=303)

    # --- Maintenance ---------------------------------------------------

    @app.get("/settings/maintenance/backup")
    def maintenance_backup(user: CurrentUser = Depends(require_admin)):
        """Télécharge un snapshot cohérent de la base Tautufin."""
        stamp = database.now_iso()[:16].replace(" ", "_").replace(":", "")
        data = database.backup_bytes()
        return Response(
            content=data,
            media_type="application/x-sqlite3",
            headers={"Content-Disposition": f'attachment; filename="tautufin-backup-{stamp}.db"'},
        )

    @app.post("/api/settings/verify-db")
    def maintenance_verify(user: CurrentUser = Depends(require_admin)):
        return database.integrity_check()

    @app.post("/api/settings/clear-image-cache")
    def maintenance_clear_image_cache(user: CurrentUser = Depends(require_admin)):
        cleared = image_cache.clear(image_cache_dir)
        logger.info("Cache d'images vidé par %s : %d fichier(s)", user.username, cleared["files"])
        return cleared

    @app.post("/settings/maintenance/reset")
    def maintenance_reset(user: CurrentUser = Depends(require_admin)):
        database.reset_data()
        return RedirectResponse("/settings?reset=1", status_code=303)
