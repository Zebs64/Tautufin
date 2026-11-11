"""Point d'entrée : CLI et application FastAPI (middlewares, gestion d'erreurs). Les
routes sont dans jellyfin_stats/web/."""

import argparse
import getpass
import logging
import sys
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles

from . import (
    __version__,
    auth,
    database,
)
from .config import Config
from .proxy import DynamicProxyHeaders
from .web import api as api_routes
from .web import auth_routes, branding, images, imports, pages, settings_routes, webhook
from .web.context import make_context
from .web.helpers import STATIC_DIR, UNSAFE_METHODS

logger = logging.getLogger(__name__)


def create_app(config: Config) -> FastAPI:
    context = make_context(config)
    scheduler, templates = context.scheduler, context.templates
    get_user, _allow_library = context.get_user, context._allow_library

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await scheduler.start()
        yield
        await scheduler.stop()

    app = FastAPI(
        title="Tautufin",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def reject_cross_origin_writes(request: Request, call_next):
        """Protection CSRF : une requête qui modifie l'état et vient d'un
        navigateur (en-tête Origin, à défaut Referer) doit viser le même hôte.
        SameSite=Lax ne couvre pas les autres sous-domaines du même site,
        courants en auto-hébergement. Sans Origin ni Referer (curl, plugin
        Webhook), la requête n'émane pas d'un navigateur et passe."""
        if request.method in UNSAFE_METHODS:
            source = request.headers.get("origin") or request.headers.get("referer")
            if source is not None:
                allowed = {
                    request.headers.get("host", ""),
                    request.headers.get("x-forwarded-host", ""),
                } - {""}
                if source == "null" or urlparse(source).netloc not in allowed:
                    logger.warning(
                        "Requête %s %s refusée : origine %r, hôte %r",
                        request.method,
                        request.url.path,
                        source,
                        sorted(allowed),
                    )
                    return JSONResponse({"detail": "Origine de la requête refusée"}, 403)
        return await call_next(request)

    # En dernier = le plus externe : IP cliente et schéma HTTPS corrigés avant
    # tout le reste. Liste de confiance relue à chaud (Réglages).
    app.add_middleware(DynamicProxyHeaders, get_trusted_proxies=lambda: config.trusted_proxies)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        is_api = request.url.path.startswith(("/api/", "/webhook"))
        if exc.status_code == 401 and not is_api:
            return RedirectResponse("/login", status_code=303)
        if exc.status_code == 403 and not is_api:
            # Spec : 403 explicite, pas de redirect silencieux.
            user = get_user(request)
            return templates.TemplateResponse(
                request,
                "error.html",
                {
                    "user": user,
                    "version": __version__,
                    "page": "",
                    "allow_library": _allow_library(user),
                    "status_code": 403,
                    "detail": exc.detail,
                },
                status_code=403,
            )
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    # Routes, dans l'ordre de déclaration d'origine (les routes génériques
    # comme /api/graphs/{graph_name} doivent rester après les spécifiques).
    for module in (
        auth_routes,
        pages,
        settings_routes,
        images,
        branding,
        api_routes,
        imports,
        webhook,
    ):
        module.register(app, context)

    return app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def reset_password_cli(username: str) -> int:
    user = auth.get_local_user(username)
    if not user:
        print(f"Compte local introuvable : {username!r}", file=sys.stderr)
        print(
            "(le reset CLI ne concerne que les comptes locaux ; les mots de"
            " passe Jellyfin se gèrent dans Jellyfin)",
            file=sys.stderr,
        )
        return 1
    password = getpass.getpass("Nouveau mot de passe : ")
    confirm = getpass.getpass("Confirmation : ")
    if not password or password != confirm:
        print("Les mots de passe ne correspondent pas.", file=sys.stderr)
        return 1
    if auth.password_error(password):
        print(auth.password_error(password), file=sys.stderr)
        return 1
    auth.set_local_password(username, password)
    print(f"Mot de passe de « {username} » réinitialisé.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="jellyfin-stats", description="Statistiques et historique de lecture pour Jellyfin"
    )
    parser.add_argument(
        "--config",
        default="config.ini",
        help="chemin du fichier de configuration (défaut: ./config.ini)",
    )
    parser.add_argument("--host", help="écrase [Web] host")
    parser.add_argument("--port", type=int, help="écrase [Web] port")
    parser.add_argument(
        "--reset-password",
        metavar="USERNAME",
        help="réinitialise le mot de passe d'un compte local puis quitte",
    )
    parser.add_argument("--debug", action="store_true", help="logs DEBUG")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )

    config = Config(args.config)
    database.init(config.database_path)

    if args.reset_password:
        return reset_password_cli(args.reset_password)

    app = create_app(config)
    uvicorn.run(
        app,
        host=args.host or config.get("Web", "host"),
        port=args.port or config.get_int("Web", "port"),
        log_level="debug" if args.debug else "info",
        # Les en-têtes X-Forwarded-* sont traités par l'appli elle-même
        # (DynamicProxyHeaders), selon [Web] trusted_proxies modifiable à chaud.
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
