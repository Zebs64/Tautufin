"""Premier lancement (setup), connexion et déconnexion."""

import logging
import threading

from fastapi import Body, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
)

from .. import (
    auth,
)
from ..jellyfin_api import JellyfinError
from ..scheduler import sync_all

logger = logging.getLogger(__name__)


def register(app: FastAPI, context) -> None:
    config = context.config
    api = context.api
    rate_limiter = context.rate_limiter
    get_user = context.get_user
    render = context.render
    is_configured = context.is_configured

    # ------------------------------------------------------------------
    # Setup (premier lancement)
    # ------------------------------------------------------------------

    @app.get("/setup")
    def setup_page(request: Request):
        if is_configured():
            return RedirectResponse("/", status_code=303)
        return render(request, "setup.html")

    @app.post("/setup")
    def setup_submit(
        request: Request,
        jellyfin_url: str = Form(""),
        api_key: str = Form(""),
        admin_username: str = Form(""),
        admin_password: str = Form(""),
    ):
        if is_configured():
            return RedirectResponse("/", status_code=303)
        jellyfin_url = jellyfin_url.strip().rstrip("/")
        errors = []
        if jellyfin_url and api_key:
            try:
                info = api.ping(base_url=jellyfin_url, api_key=api_key.strip())
                logger.info(
                    "Connexion Jellyfin OK : %s %s", info.get("ServerName"), info.get("Version")
                )
            except JellyfinError as exc:
                errors.append(f"Connexion Jellyfin impossible : {exc}")
        elif jellyfin_url or api_key:
            errors.append("URL Jellyfin et clé API doivent être fournies ensemble.")
        if not admin_username or not admin_password:
            errors.append("Le compte admin local de secours est obligatoire.")
        elif auth.password_error(admin_password):
            errors.append(auth.password_error(admin_password))
        if errors:
            return render(
                request,
                "setup.html",
                errors=errors,
                form={"jellyfin_url": jellyfin_url, "admin_username": admin_username},
            )
        if jellyfin_url:
            config.set("Jellyfin", "url", jellyfin_url)
            config.set("Jellyfin", "api_key", api_key.strip())
            config.save()
        auth.create_local_user(admin_username.strip(), admin_password, role="admin")
        if config.jellyfin_configured:
            threading.Thread(target=_safe_sync, daemon=True).start()
        return RedirectResponse("/login", status_code=303)

    def _safe_sync():
        try:
            sync_all(api)
        except JellyfinError as exc:
            logger.warning("Synchronisation initiale impossible : %s", exc)

    # ------------------------------------------------------------------
    # Login / logout
    # ------------------------------------------------------------------

    @app.get("/login")
    def login_page(request: Request):
        if not is_configured():
            return RedirectResponse("/setup", status_code=303)
        if get_user(request):
            return RedirectResponse("/", status_code=303)
        return render(
            request,
            "login.html",
            jellyfin_enabled=config.jellyfin_auth_enabled and config.jellyfin_configured,
            local_enabled=config.local_auth_enabled,
            embed_origins=_embed_origins(),
        )

    def _embed_origins() -> list[str]:
        """Origines de Jellyfin Web dont la page de connexion, intégrée dans
        un onglet personnalisé, accepte la session (liste vide = désactivé)."""
        return config.jellyfin_web_origins if config.jellyfin_embed_login_enabled else []

    def _set_session_cookie(response, request: Request, user: auth.CurrentUser) -> None:
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.cookie_value(user),
            max_age=config.session_lifetime,
            httponly=True,
            samesite="lax",
            # HTTPS direct, ou via un proxy listé dans [Web] trusted_proxies.
            secure=request.url.scheme == "https",
        )

    def _check_rate_limit(request: Request) -> None:
        ip = request.client.host if request.client else "?"
        if not rate_limiter.check(ip):
            logger.warning("Rate limit login dépassé pour %s", ip)
            raise HTTPException(
                status_code=429, detail="Trop de tentatives, réessayez dans une minute."
            )

    @app.post("/login")
    def login_submit(
        request: Request,
        mode: str = Form("jellyfin"),
        username: str = Form(...),
        password: str = Form(...),
    ):
        _check_rate_limit(request)
        user = None
        try:
            if mode == "jellyfin" and config.jellyfin_auth_enabled and config.jellyfin_configured:
                user = auth.login_jellyfin(api, username.strip(), password)
            elif mode == "local" and config.local_auth_enabled:
                user = auth.login_local(username.strip(), password)
        except auth.AccessBlocked:
            logger.warning("Login refusé (accès bloqué) : %s", username.strip())
            return render(
                request,
                "login.html",
                error="Votre accès à Tautufin a été désactivé.",
                active_tab=mode,
                jellyfin_enabled=config.jellyfin_auth_enabled and config.jellyfin_configured,
                local_enabled=config.local_auth_enabled,
                embed_origins=_embed_origins(),
            )
        if user is None:
            return render(
                request,
                "login.html",
                error="Identifiants invalides.",
                active_tab=mode,
                jellyfin_enabled=config.jellyfin_auth_enabled and config.jellyfin_configured,
                local_enabled=config.local_auth_enabled,
                embed_origins=_embed_origins(),
            )
        response = RedirectResponse("/", status_code=303)
        _set_session_cookie(response, request, user)
        logger.info("Login %s réussi : %s (%s)", mode, user.username, user.role)
        return response

    @app.post("/login/jellyfin-session")
    def login_jellyfin_session(request: Request, token: str = Body("", embed=True)):
        """Connexion automatique depuis un onglet personnalisé de Jellyfin :
        la page de connexion reçoit par postMessage le jeton de la session
        Jellyfin Web parente et le transmet ici. Jellyfin dit à qui il
        appartient ; le jeton n'est ni stocké ni journalisé."""
        if not config.jellyfin_embed_login_enabled:
            raise HTTPException(status_code=404, detail="Connexion automatique désactivée.")
        _check_rate_limit(request)
        try:
            user = auth.login_jellyfin_token(api, token)
        except auth.AccessBlocked:
            logger.warning("Connexion automatique refusée (accès bloqué)")
            # Réponses JSON directes : le gestionnaire d'erreurs des pages
            # changerait un 401 en redirection vers /login.
            return JSONResponse({"detail": "Votre accès à Tautufin a été désactivé."}, 403)
        if user is None:
            return JSONResponse({"detail": "Session Jellyfin non reconnue."}, 401)
        response = JSONResponse({"ok": True})
        _set_session_cookie(response, request, user)
        logger.info("Login jellyfin (onglet intégré) réussi : %s (%s)", user.username, user.role)
        return response

    @app.post("/logout")
    def logout(request: Request):
        user = get_user(request)
        if user:
            auth.destroy_session(user.token)
        # « manual » : pas de reconnexion automatique juste après une
        # déconnexion volontaire (onglet personnalisé de Jellyfin).
        response = RedirectResponse("/login?manual=1", status_code=303)
        response.delete_cookie(auth.SESSION_COOKIE)
        return response
