"""Point d'entrée : CLI, application FastAPI, routes pages + API + webhook."""

import argparse
import getpass
import logging
import re
import secrets
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import (Depends, FastAPI, File, Form, HTTPException, Query,
                     Request, UploadFile)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (__version__, auth, database, graphs, history, import_playback,
               import_streamystats, infer_history, media)
from . import libraries as libraries_mod
from . import users as users_mod
from .activity import ActivityMonitor
from .auth import CurrentUser, RateLimiter
from .config import Config
from .jellyfin_api import JellyfinAPI, JellyfinError
from .scheduler import Scheduler, sync_all

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "data" / "interfaces" / "default" / "templates"
STATIC_DIR = BASE_DIR / "data" / "interfaces" / "default" / "static"

MEDIA_TYPES = ["Movie", "Episode", "Audio", "MusicVideo", "AudioBook"]
PERIOD_DAYS = {"24h": 1, "7d": 7, "30d": 30, "12m": 365, "all": None}

MEDIA_ICONS = {"Movie": "🎬", "Episode": "📺", "Series": "📺", "Season": "📺",
               "Audio": "🎵", "MusicAlbum": "💿", "MusicVideo": "🎤",
               "AudioBook": "🎧"}

CLIENT_ICONS = [  # mapping par sous-chaîne, premier qui matche
    (("android tv", "androidtv", "apple tv", "tvos", "roku", "tizen",
      "webos", "samsung", "lg tv", "shield"), "📺"),
    (("web", "browser", "chrome", "firefox", "edge", "safari", "opera"), "🌐"),
    (("iphone", "ipad", "ios", "swiftfin"), "📱"),
    (("android", "findroid"), "🤖"),
    (("kodi", "infuse", "emby", "plex"), "🎦"),
    (("mpv", "vlc", "mediaplayer"), "🎞️"),
    (("dlna", "cast", "chromecast"), "📡"),
]


LIBRARY_ICONS = {"movies": "🎬", "tvshows": "📺", "music": "🎵",
                 "musicvideos": "🎤", "books": "📚", "homevideos": "🎥",
                 "photos": "🖼️", "boxsets": "🎞️", "livetv": "📡",
                 "playlists": "🎼"}


def media_icon(media_type: str) -> str:
    return MEDIA_ICONS.get(media_type or "", "🎞️")


def library_icon(collection_type: str) -> str:
    return LIBRARY_ICONS.get((collection_type or "").lower(), "🗂️")


def client_icon(client_name: str) -> str:
    name = (client_name or "").lower()
    for keywords, icon in CLIENT_ICONS:
        if any(k in name for k in keywords):
            return icon
    return "💻"


def _browser_slug(s: str) -> str | None:
    if "edg" in s:
        return "edge"
    if "firefox" in s or "fxios" in s:
        return "firefox"
    if "opera" in s or "opr" in s:
        return "opera"
    if "brave" in s:
        return "brave"
    if "safari" in s and "chrome" not in s and "crios" not in s:
        return "safari"
    if "chrome" in s or "chromium" in s or "crios" in s:
        return "chrome"
    return None


def client_slug(product: str, device: str = "") -> str:
    """Logo officiel d'un client (miroir de clientSlug côté JS). Le navigateur
    est déduit du device, la plateforme du produit ; repli sur le logo Jellyfin."""
    p = (product or "").lower()
    d = (device or "").lower()
    if "web" in p or "browser" in p:
        return _browser_slug(d) or _browser_slug(p) or "jellyfin"
    if "wholphin" in p:
        return "wholphin"
    if any(k in p for k in ("ios", "ipad", "iphone", "tvos", "mac", "apple",
                            "swiftfin", "infuse")):
        return "apple"
    if "kodi" in p:
        return "kodi"
    if "plex" in p:
        return "plex"
    if "chromecast" in p or "cast" in p or "google tv" in p:
        return "googletv"
    if "android" in p:
        return "android"
    if "samsung" in p or "tizen" in p:
        return "samsung"
    if "windows" in p:
        return "windows"
    if "media player" in p or "mpv" in p or "jellyfin" in p:
        return "jellyfin"
    if "shield" in d or "nvidia" in d:
        return "nvidia"
    return _browser_slug(d) or "jellyfin"


def client_logo(client_name: str) -> str:
    return f"/static/img/clients/{client_slug(client_name)}.svg"

# Identifiants Jellyfin (GUID avec ou sans tirets) — borne aussi les noms de
# fichiers du cache d'images.
JELLYFIN_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
BRANDING_EXTS = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".ico", ".gif"}
BRANDING_MAX_BYTES = 2 * 1024 * 1024
IMPORT_UPLOAD_MAX_BYTES = 512 * 1024 * 1024  # backups .tsv/.db volumineux
IMAGE_CACHE_HEADERS = {"Cache-Control": "public, max-age=86400"}


def _sniff_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.lstrip()[:5] in (b"<svg ", b"<?xml"):
        return "image/svg+xml"
    return "application/octet-stream"


# ---------------------------------------------------------------------------
# Helpers d'application
# ---------------------------------------------------------------------------

def _duration_hm(seconds) -> str:
    seconds = int(seconds or 0)
    hours, minutes = divmod(seconds // 60, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["duration_hm"] = _duration_hm
    templates.env.filters["dt"] = lambda v: (v or "")[:16]
    templates.env.filters["media_icon"] = media_icon
    templates.env.filters["library_icon"] = library_icon
    templates.env.filters["client_icon"] = client_icon
    templates.env.filters["client_logo"] = client_logo
    return templates


def create_app(config: Config) -> FastAPI:
    api = JellyfinAPI(config)
    monitor = ActivityMonitor(config)
    scheduler = Scheduler(config, api, monitor)
    rate_limiter = RateLimiter()
    templates = make_templates()
    auth.init(config.secret_key, config.session_lifetime)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await scheduler.start()
        yield
        await scheduler.stop()

    app = FastAPI(title="Tautufin", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    config_dir = Path(config.path).parent
    image_cache_dir = config_dir / "cache" / "images"
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

    def require_library_access(user: CurrentUser = Depends(require_user)) -> CurrentUser:
        if not user.is_admin and not config.allow_user_library_pages:
            raise HTTPException(status_code=403,
                                detail="Pages bibliothèques désactivées par l'admin")
        return user

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        is_api = request.url.path.startswith(("/api/", "/webhook"))
        if exc.status_code == 401 and not is_api:
            return RedirectResponse("/login", status_code=303)
        if exc.status_code == 403 and not is_api:
            # Spec : 403 explicite, pas de redirect silencieux.
            user = get_user(request)
            return templates.TemplateResponse(
                request, "error.html",
                {"user": user, "version": __version__, "page": "",
                 "allow_library": _allow_library(user),
                 "status_code": 403, "detail": exc.detail},
                status_code=403)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

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
        ctx.update(user=user, version=__version__,
                   allow_library=_allow_library(user),
                   jellyfin_configured=config.jellyfin_configured,
                   logo_ver=int(logo.stat().st_mtime) if logo else 0,
                   has_logo=logo is not None)
        return templates.TemplateResponse(request, template, ctx)

    def is_configured() -> bool:
        return config.jellyfin_configured or auth.has_local_admin()

    def scoped_user_id(user: CurrentUser, requested: str | None) -> str | None:
        """Isolation des données : un non-admin ne voit que son UserId,
        imposé depuis la session serveur (jamais depuis le client)."""
        if user.is_admin:
            return requested or None
        return user.jellyfin_user_id or "__none__"

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
                logger.info("Connexion Jellyfin OK : %s %s",
                            info.get("ServerName"), info.get("Version"))
            except JellyfinError as exc:
                errors.append(f"Connexion Jellyfin impossible : {exc}")
        elif jellyfin_url or api_key:
            errors.append("URL Jellyfin et clé API doivent être fournies ensemble.")
        if not admin_username or not admin_password:
            errors.append("Le compte admin local de secours est obligatoire.")
        if errors:
            return render(request, "setup.html", errors=errors,
                          form={"jellyfin_url": jellyfin_url,
                                "admin_username": admin_username})
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
        return render(request, "login.html",
                      jellyfin_enabled=config.jellyfin_auth_enabled and config.jellyfin_configured,
                      local_enabled=config.local_auth_enabled)

    @app.post("/login")
    def login_submit(
        request: Request,
        mode: str = Form("jellyfin"),
        username: str = Form(...),
        password: str = Form(...),
    ):
        ip = request.client.host if request.client else "?"
        if not rate_limiter.check(ip):
            logger.warning("Rate limit login dépassé pour %s", ip)
            raise HTTPException(status_code=429,
                                detail="Trop de tentatives, réessayez dans une minute.")
        user = None
        if mode == "jellyfin" and config.jellyfin_auth_enabled and config.jellyfin_configured:
            user = auth.login_jellyfin(api, username.strip(), password)
        elif mode == "local" and config.local_auth_enabled:
            user = auth.login_local(username.strip(), password)
        if user is None:
            return render(request, "login.html",
                          error="Identifiants invalides.", active_tab=mode,
                          jellyfin_enabled=config.jellyfin_auth_enabled and config.jellyfin_configured,
                          local_enabled=config.local_auth_enabled)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            auth.SESSION_COOKIE, auth.cookie_value(user),
            max_age=config.session_lifetime, httponly=True, samesite="lax")
        logger.info("Login %s réussi : %s (%s)", mode, user.username, user.role)
        return response

    @app.get("/logout")
    def logout(request: Request):
        user = get_user(request)
        if user:
            auth.destroy_session(user.token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.SESSION_COOKIE)
        return response

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
        jf_users = users_mod.list_users_with_stats() if user.is_admin else []
        libs = database.query("SELECT library_id, name FROM libraries ORDER BY name")
        return render(request, "history.html", user=user, page="history",
                      jf_users=jf_users, libs=libs, media_types=MEDIA_TYPES)

    @app.get("/media/{item_id}")
    def media_detail_page(request: Request, item_id: str,
                          user: CurrentUser = Depends(require_user)):
        if not JELLYFIN_ID_RE.match(item_id):
            raise HTTPException(status_code=404, detail="Média inconnu")
        # Isolation : un non-admin ne voit que ses propres visionnages.
        detail = media.media_overview(item_id, user_id=scoped_user_id(user, None))
        if not detail:
            raise HTTPException(status_code=404, detail="Média inconnu")
        return render(request, "media.html", user=user, page="history",
                      media=detail)

    @app.get("/graphs")
    def graphs_page(request: Request, user: CurrentUser = Depends(require_user)):
        jf_users = users_mod.list_users_with_stats() if user.is_admin else []
        return render(request, "graphs.html", user=user, page="graphs",
                      jf_users=jf_users, years=graphs.available_years())

    @app.get("/users")
    def users_page(request: Request, user: CurrentUser = Depends(require_admin)):
        return render(request, "users.html", user=user, page="users",
                      users=users_mod.list_users_with_stats())

    @app.get("/users/{jellyfin_user_id}")
    def user_detail_page(request: Request, jellyfin_user_id: str,
                         user: CurrentUser = Depends(require_user)):
        if not user.is_admin and jellyfin_user_id != user.jellyfin_user_id:
            raise HTTPException(status_code=403,
                                detail="Vous ne pouvez consulter que votre propre profil.")
        return render(request, "user.html", user=user, page="users",
                      profile=users_mod.user_overview(jellyfin_user_id))

    @app.get("/profile")
    def profile_page(request: Request, user: CurrentUser = Depends(require_user)):
        if not user.jellyfin_user_id:
            return render(request, "user.html", user=user, page="profile",
                          profile=None)
        return render(request, "user.html", user=user, page="profile",
                      profile=users_mod.user_overview(user.jellyfin_user_id))

    @app.get("/libraries")
    def libraries_page(request: Request,
                       user: CurrentUser = Depends(require_library_access)):
        return render(request, "libraries.html", user=user, page="libraries",
                      libraries=libraries_mod.list_libraries_with_stats())

    @app.get("/libraries/{library_id}")
    def library_detail_page(request: Request, library_id: str,
                            user: CurrentUser = Depends(require_library_access)):
        library = libraries_mod.get_library(library_id)
        if not library:
            raise HTTPException(status_code=404, detail="Bibliothèque inconnue")
        return render(request, "library.html", user=user, page="libraries",
                      library=library, detail=libraries_mod.library_detail(library_id))

    @app.get("/import")
    def import_page(request: Request, user: CurrentUser = Depends(require_admin)):
        return render(request, "import.html", user=user, page="import",
                      minimum_duration=config.minimum_duration,
                      minimum_percent=config.minimum_percent,
                      jf_users=database.query(
                          "SELECT jellyfin_user_id, username FROM users"
                          " ORDER BY username"),
                      inferred_count=infer_history.count_inferred())

    # ------------------------------------------------------------------
    # Settings (admin)
    # ------------------------------------------------------------------

    def _render_settings(request, user, **ctx):
        return render(request, "settings.html", user=user, page="settings",
                      config=config, local_users=auth.list_local_users(),
                      jf_users=database.query(
                          "SELECT jellyfin_user_id, username FROM users ORDER BY username"),
                      **ctx)

    @app.get("/settings")
    def settings_page(request: Request, saved: int = 0,
                      user: CurrentUser = Depends(require_admin)):
        return _render_settings(request, user, saved=bool(saved))

    @app.post("/settings")
    def settings_submit(
        request: Request,
        user: CurrentUser = Depends(require_admin),
        jellyfin_url: str = Form(""),
        api_key: str = Form(""),
        session_lifetime: int = Form(604800),
        jellyfin_auth_enabled: str = Form(None),
        local_auth_enabled: str = Form(None),
        minimum_duration: int = Form(300),
        minimum_percent: int = Form(0),
        poll_interval: int = Form(15),
        sync_interval: int = Form(3600),
        session_grace: int = Form(90),
        allow_user_library_pages: str = Form(None),
    ):
        config.set("Jellyfin", "url", jellyfin_url.strip().rstrip("/"))
        if api_key.strip():  # champ vide = clé inchangée
            config.set("Jellyfin", "api_key", api_key.strip())
        config.set("Auth", "session_lifetime", max(300, session_lifetime))
        config.set("Auth", "jellyfin_auth_enabled", bool(jellyfin_auth_enabled))
        config.set("Auth", "local_auth_enabled", bool(local_auth_enabled))
        config.set("Monitoring", "minimum_duration", max(0, minimum_duration))
        config.set("Monitoring", "minimum_percent", min(100, max(0, minimum_percent)))
        config.set("Monitoring", "poll_interval", max(5, poll_interval))
        config.set("Monitoring", "sync_interval", max(60, sync_interval))
        config.set("Monitoring", "session_grace", max(0, session_grace))
        config.set("UI", "allow_user_library_pages", bool(allow_user_library_pages))
        config.save()
        auth.init(config.secret_key, config.session_lifetime)
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
            return _render_settings(request, user,
                                    user_error="Nom et mot de passe obligatoires.")
        if auth.get_local_user(username):
            return _render_settings(request, user,
                                    user_error=f"Le compte « {username} » existe déjà.")
        auth.create_local_user(username, password,
                               role if role in ("admin", "user") else "user",
                               jellyfin_user_id.strip() or None)
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/local-users/{local_id}/delete")
    def delete_local_user_route(local_id: int,
                                user: CurrentUser = Depends(require_admin)):
        auth.delete_local_user(local_id)
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/local-users/{local_id}/password")
    def reset_local_password_route(
        local_id: int,
        user: CurrentUser = Depends(require_admin),
        new_password: str = Form(...),
    ):
        row = database.query_one("SELECT username FROM local_users WHERE id = ?",
                                 (local_id,))
        if row and new_password:
            auth.set_local_password(row["username"], new_password)
        return RedirectResponse("/settings", status_code=303)

    # ------------------------------------------------------------------
    # Proxy d'images Jellyfin (posters, avatars) avec cache disque
    # ------------------------------------------------------------------
    # Le navigateur ne parle jamais à Jellyfin : la clé API reste côté
    # serveur. Cache négatif (fichier vide) pour ne pas marteler Jellyfin
    # sur les médias sans image.

    def _cached_fetch(cache_key: str, fetch) -> bytes | None:
        image_cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = image_cache_dir / cache_key
        if cache_file.exists():
            return cache_file.read_bytes() or None
        result = fetch() if config.jellyfin_configured else None
        if result:
            cache_file.write_bytes(result[0])
            return result[0]
        if config.jellyfin_configured:  # négatif seulement si réponse réelle
            cache_file.write_bytes(b"")
        return None

    def _image_response(data: bytes | None, placeholder: str) -> Response:
        if data:
            return Response(data, media_type=_sniff_mime(data),
                            headers=IMAGE_CACHE_HEADERS)
        return FileResponse(STATIC_DIR / "img" / placeholder,
                            headers=IMAGE_CACHE_HEADERS)

    @app.get("/image/item/{item_id}")
    def image_item(item_id: str,
                   fallback: str | None = Query(None),
                   w: int = Query(480, ge=40, le=1200),
                   type: str = Query("Primary"),
                   user: CurrentUser = Depends(require_user)):
        # Backdrop (fond « hero » des cartes du dashboard) : on tente l'art
        # paysage du média ; s'il est absent on retombe sur le poster.
        if type == "Backdrop" and JELLYFIN_ID_RE.match(item_id):
            data = _cached_fetch(
                f"item_{item_id}_bd_{w}",
                lambda: api.get_image(item_id, image_type="Backdrop",
                                      max_width=w))
            if data is not None:
                return _image_response(data, "poster-placeholder.svg")
        data = None
        if JELLYFIN_ID_RE.match(item_id):
            data = _cached_fetch(f"item_{item_id}_{w}",
                                 lambda: api.get_image(item_id, max_width=w))
        if data is None and fallback and JELLYFIN_ID_RE.match(fallback):
            data = _cached_fetch(f"item_{fallback}_{w}",
                                 lambda: api.get_image(fallback, max_width=w))
        if data is None and not fallback and JELLYFIN_ID_RE.match(item_id):
            # Épisode sans image propre : tenter le poster de sa série.
            series = database.query_one(
                """
                SELECT s.item_id AS id FROM items e
                JOIN items s ON s.type = 'Series' AND s.name = e.series_name
                WHERE e.item_id = ? LIMIT 1
                """,
                (item_id,))
            if series:
                data = _cached_fetch(
                    f"item_{series['id']}_{w}",
                    lambda: api.get_image(series["id"], max_width=w))
        return _image_response(data, "poster-placeholder.svg")

    @app.get("/image/user/{user_id}")
    def image_user(user_id: str, user: CurrentUser = Depends(require_user)):
        data = None
        if JELLYFIN_ID_RE.match(user_id):
            data = _cached_fetch(f"user_{user_id}",
                                 lambda: api.get_user_image(user_id))
        return _image_response(data, "avatar-placeholder.svg")

    # ------------------------------------------------------------------
    # Branding : logo et favicon personnalisables (stockés dans /config)
    # ------------------------------------------------------------------

    @app.get("/branding/logo")
    def branding_logo():
        f = _branding_file("logo")
        if not f:
            raise HTTPException(status_code=404, detail="Pas de logo personnalisé")
        return FileResponse(f, headers=IMAGE_CACHE_HEADERS)

    @app.get("/branding/favicon")
    @app.get("/favicon.ico")
    def branding_favicon():
        f = _branding_file("favicon") or STATIC_DIR / "img" / "favicon.svg"
        return FileResponse(f, headers=IMAGE_CACHE_HEADERS)

    def _save_branding(name: str, upload: UploadFile) -> str | None:
        """Enregistre logo/favicon ; retourne un message d'erreur ou None."""
        ext = Path(upload.filename or "").suffix.lower()
        if ext not in BRANDING_EXTS:
            return (f"Format non supporté ({ext or 'sans extension'}) — "
                    f"acceptés : {', '.join(sorted(BRANDING_EXTS))}")
        data = upload.file.read(BRANDING_MAX_BYTES + 1)
        if not data:
            return None  # champ laissé vide : on ne touche à rien
        if len(data) > BRANDING_MAX_BYTES:
            return "Fichier trop volumineux (max 2 Mo)"
        branding_dir.mkdir(parents=True, exist_ok=True)
        old = _branding_file(name)
        if old:
            old.unlink()
        (branding_dir / f"{name}{ext}").write_bytes(data)
        return None

    @app.post("/settings/branding")
    def upload_branding(request: Request,
                        user: CurrentUser = Depends(require_admin),
                        logo: UploadFile = File(None),
                        favicon: UploadFile = File(None)):
        errors = []
        for name, upload in (("logo", logo), ("favicon", favicon)):
            if upload and upload.filename:
                error = _save_branding(name, upload)
                if error:
                    errors.append(f"{name} : {error}")
        if errors:
            return _render_settings(request, user, branding_error=" — ".join(errors))
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/branding/delete")
    def delete_branding(user: CurrentUser = Depends(require_admin),
                        which: str = Form(...)):
        if which in ("logo", "favicon"):
            f = _branding_file(which)
            if f:
                f.unlink()
        return RedirectResponse("/settings", status_code=303)

    # ------------------------------------------------------------------
    # API JSON
    # ------------------------------------------------------------------

    @app.get("/api/activity")
    def api_activity(user: CurrentUser = Depends(require_user)):
        sessions = monitor.snapshot(scoped_user_id(user, None))
        if not user.is_admin:
            for s in sessions:  # un non-admin ne voit pas les détails réseau
                s.pop("jellyfin_user_id", None)
                s.pop("ip_address", None)
                s.pop("is_lan", None)
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
            media_type=media_type, library_id=library_id,
            date_from=date_from, date_to=date_to, search=search,
            sort=sort, order=order, page=page, page_size=page_size)
        if not user.is_admin:
            for row in result["rows"]:
                row.pop("ip_address", None)
        return result

    @app.get("/api/home_stats")
    def api_home_stats(user: CurrentUser = Depends(require_user),
                       period: str = Query("7d"),
                       metric: str = Query("plays")):
        days = PERIOD_DAYS.get(period, 7)  # None = depuis toujours
        metric = metric if metric in ("plays", "duration") else "plays"
        uid = scoped_user_id(user, None)

        def flat(data):
            series = data["series"][0]["data"] if data["series"] else []
            return [{"label": c, "value": v}
                    for c, v in zip(data["categories"], series)]

        stats = {
            "metric": metric,
            "top_movies": graphs.top_media(days, "movie", metric, uid, limit=10),
            "top_series": graphs.top_media(days, "series", metric, uid, limit=10),
            "recently_watched": graphs.recently_watched(days, uid, limit=10),
            "top_libraries": graphs.top_libraries(days, metric, uid, limit=10),
            "top_clients": flat(graphs.by_client(days, metric, uid)),
        }
        # « Populaires » = nb d'utilisateurs distincts : pertinent uniquement en
        # vue globale (un non-admin n'a que ses propres données → toujours 1).
        if user.is_admin:
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
        return graphs.plays_over_time(days or None, group, metric,
                                      scoped_user_id(user, user_id), year)

    GRAPH_FUNCS = {
        "by_library": graphs.by_library,
        "by_genre": graphs.by_genre,
        "by_resolution": graphs.by_resolution,
        "by_client": graphs.by_client,
        "by_play_method": graphs.by_play_method,
        "by_day_of_week": graphs.by_day_of_week,
        "by_hour_of_day": graphs.by_hour_of_day,
    }

    @app.get("/api/graphs/by_user")
    def api_graph_by_user(user: CurrentUser = Depends(require_admin),
                          days: int = DaysParam,
                          metric: str = Query("plays"),
                          year: int | None = YearParam):
        return graphs.by_user(days or None, metric, year)

    @app.get("/api/graphs/top_items")
    def api_graph_top_items(user: CurrentUser = Depends(require_user),
                            days: int = DaysParam,
                            kind: str = Query("movie"),
                            metric: str = Query("plays"),
                            user_id: str | None = Query(None),
                            year: int | None = YearParam):
        return graphs.top_items(days or None, kind, metric,
                                scoped_user_id(user, user_id), year=year)

    @app.get("/api/graphs/top_people")
    def api_graph_top_people(user: CurrentUser = Depends(require_user),
                             days: int = DaysParam,
                             kind: str = Query("actor"),
                             metric: str = Query("plays"),
                             user_id: str | None = Query(None),
                             year: int | None = YearParam):
        return graphs.top_people(days or None, kind, metric,
                                 scoped_user_id(user, user_id), year=year)

    @app.get("/api/graphs/{graph_name}")
    def api_graph(graph_name: str,
                  user: CurrentUser = Depends(require_user),
                  days: int = DaysParam,
                  metric: str = Query("plays"),
                  user_id: str | None = Query(None),
                  year: int | None = YearParam):
        func = GRAPH_FUNCS.get(graph_name)
        if func is None:
            raise HTTPException(status_code=404, detail="Graphique inconnu")
        return func(days or None, metric, scoped_user_id(user, user_id), year)

    @app.get("/api/users")
    def api_users(user: CurrentUser = Depends(require_admin)):
        return {"users": users_mod.list_users_with_stats()}

    @app.post("/api/settings/test-jellyfin")
    def api_test_jellyfin(payload: dict,
                          user: CurrentUser = Depends(require_admin)):
        try:
            info = api.ping(base_url=(payload.get("url") or "").strip().rstrip("/"),
                            api_key=(payload.get("api_key") or "").strip()
                                    or config.jellyfin_api_key)
            return {"ok": True,
                    "server": info.get("ServerName"), "version": info.get("Version")}
        except JellyfinError as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/sync")
    def api_sync(user: CurrentUser = Depends(require_admin)):
        if not config.jellyfin_configured:
            raise HTTPException(status_code=400, detail="Jellyfin non configuré")
        try:
            return sync_all(api)
        except JellyfinError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/api/import/upload")
    def api_import_upload(user: CurrentUser = Depends(require_admin),
                          file: UploadFile = File(...)):
        """Réception d'un backup .tsv ou d'un playback_reporting.db envoyé
        depuis le navigateur. Stocké dans <config>/uploads/ (un seul fichier
        conservé), puis analysé/importé via son chemin comme un fichier local."""
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_",
                           Path(file.filename or "").name).lstrip(".") or "backup-upload"
        uploads_dir = config_dir / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        # Écriture dans un .tmp : l'upload précédent n'est remplacé qu'une
        # fois le nouveau reçu et validé.
        tmp = uploads_dir / f".upload-{secrets.token_hex(6)}.tmp"
        size = 0
        try:
            with open(tmp, "wb") as out:
                while chunk := file.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > IMPORT_UPLOAD_MAX_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail="Fichier trop volumineux (max 512 Mo)")
                    out.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail="Fichier vide")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        for old in uploads_dir.iterdir():  # on ne garde que le dernier upload
            if old.is_file() and old != tmp:
                old.unlink()
        dest = uploads_dir / safe_name
        tmp.rename(dest)
        logger.info("Backup uploadé par %s : %s (%d octets)",
                    user.username, safe_name, size)
        return {"path": str(dest), "filename": safe_name, "size": size}

    @app.post("/api/import/analyze")
    def api_import_analyze(payload: dict,
                           user: CurrentUser = Depends(require_admin)):
        # Détection automatique : backup JSON Streamystats vs base/backup
        # Playback Reporting (.db/.tsv).
        path = (payload.get("path") or "").strip()
        try:
            if import_streamystats.looks_like_streamystats(path):
                return import_streamystats.analyze(path)
            return import_playback.analyze(path)
        except import_playback.ImportError_ as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/import/run")
    def api_import_run(payload: dict,
                       user: CurrentUser = Depends(require_admin)):
        path = (payload.get("path") or "").strip()
        module = (import_streamystats
                  if import_streamystats.looks_like_streamystats(path)
                  else import_playback)
        try:
            return module.run_import(
                path, api, config.minimum_duration, config.minimum_percent)
        except import_playback.ImportError_ as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------------
    # Inférence d'historique depuis le statut « Lu » de Jellyfin (admin)
    # ------------------------------------------------------------------

    @app.post("/api/infer/run")
    def api_infer_run(payload: dict,
                      user: CurrentUser = Depends(require_admin)):
        if not config.jellyfin_configured:
            raise HTTPException(status_code=400, detail="Jellyfin non configuré")
        target = (payload.get("user_id") or "").strip() or None
        report = infer_history.infer_history(
            api, config.minimum_duration, config.minimum_percent, user_id=target)
        report["inferred_count"] = infer_history.count_inferred()
        return report

    @app.post("/api/infer/cleanup")
    def api_infer_cleanup(payload: dict,
                          user: CurrentUser = Depends(require_admin)):
        target = (payload.get("user_id") or "").strip() or None
        deleted = infer_history.delete_inferred(user_id=target)
        return {"deleted": deleted,
                "inferred_count": infer_history.count_inferred()}

    # ------------------------------------------------------------------
    # Webhook Jellyfin (plugin Webhook, événements temps réel)
    # ------------------------------------------------------------------

    @app.post("/webhook")
    def webhook(payload: dict):
        try:
            monitor.handle_webhook(payload)
        except Exception:
            logger.exception("Erreur de traitement du webhook")
        return {"ok": True}

    return app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def reset_password_cli(username: str) -> int:
    user = auth.get_local_user(username)
    if not user:
        print(f"Compte local introuvable : {username!r}", file=sys.stderr)
        print("(le reset CLI ne concerne que les comptes locaux ; les mots de"
              " passe Jellyfin se gèrent dans Jellyfin)", file=sys.stderr)
        return 1
    password = getpass.getpass("Nouveau mot de passe : ")
    confirm = getpass.getpass("Confirmation : ")
    if not password or password != confirm:
        print("Les mots de passe ne correspondent pas.", file=sys.stderr)
        return 1
    auth.set_local_password(username, password)
    print(f"Mot de passe de « {username} » réinitialisé.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="jellyfin-stats",
        description="Statistiques et historique de lecture pour Jellyfin")
    parser.add_argument("--config", default="config.ini",
                        help="chemin du fichier de configuration (défaut: ./config.ini)")
    parser.add_argument("--host", help="écrase [Web] host")
    parser.add_argument("--port", type=int, help="écrase [Web] port")
    parser.add_argument("--reset-password", metavar="USERNAME",
                        help="réinitialise le mot de passe d'un compte local puis quitte")
    parser.add_argument("--debug", action="store_true", help="logs DEBUG")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s")

    config = Config(args.config)
    database.init(config.database_path)

    if args.reset_password:
        return reset_password_cli(args.reset_password)

    app = create_app(config)
    uvicorn.run(app,
                host=args.host or config.get("Web", "host"),
                port=args.port or config.get_int("Web", "port"),
                log_level="debug" if args.debug else "info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
