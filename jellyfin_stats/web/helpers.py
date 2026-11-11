"""Constantes et utilitaires partagés par les routes (icônes, MIME, CSV, templates)."""

import re
import zlib
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .. import __version__
from ..labels import collection_label, date_long_label, datetime_label, media_type_label

BASE_DIR = Path(__file__).resolve().parents[2]  # racine du dépôt (jellyfin_stats/web/ -> ..)
TEMPLATES_DIR = BASE_DIR / "data" / "interfaces" / "default" / "templates"
STATIC_DIR = BASE_DIR / "data" / "interfaces" / "default" / "static"

MEDIA_TYPES = ["Movie", "Episode", "Audio", "MusicVideo", "AudioBook"]
PERIOD_DAYS = {"24h": 1, "7d": 7, "30d": 30, "12m": 365, "all": None}

# Icônes : noms de symboles du sprite Material (static/img/icons.svg), même
# style que l'interface Jellyfin.
MEDIA_ICONS = {
    "Movie": "movie",
    "Episode": "tv",
    "Series": "tv",
    "Season": "tv",
    "Audio": "music_note",
    "MusicAlbum": "album",
    "MusicVideo": "music_video",
    "AudioBook": "headphones",
}

LIBRARY_ICONS = {
    "movies": "movie",
    "tvshows": "tv",
    "music": "library_music",
    "musicvideos": "music_video",
    "books": "menu_book",
    "homevideos": "videocam",
    "photos": "photo_library",
    "boxsets": "collections_bookmark",
    "livetv": "live_tv",
    "playlists": "queue_music",
}


def icon(name: str, cls: str = "") -> Markup:
    """Icône SVG du sprite (taille et couleur du texte, décorative)."""
    classes = f"icon {cls}".strip()
    return Markup(
        f'<svg class="{escape(classes)}" aria-hidden="true" focusable="false">'
        f'<use href="/static/img/icons.svg?v={__version__}#{escape(name)}"></use></svg>'
    )


def media_icon(media_type: str) -> Markup:
    return icon(MEDIA_ICONS.get(media_type or "", "play_circle"))


def library_icon(collection_type: str) -> Markup:
    return icon(LIBRARY_ICONS.get((collection_type or "").lower(), "folder"))


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
    if any(k in p for k in ("ios", "ipad", "iphone", "tvos", "mac", "apple", "swiftfin", "infuse")):
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


def _csv_cell(value):
    """Valeur exportée en CSV. Un texte commençant par = + - @ (ou tabulation,
    retour chariot) serait interprété comme une formule par un tableur : les
    noms d'appareil, réglables par tout utilisateur Jellyfin, le permettraient.
    On le préfixe d'une apostrophe."""
    if value is None:
        return ""
    if isinstance(value, float):
        return round(value, 1)
    text = str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return value


def client_logo(client_name: str) -> str:
    return f"/static/img/clients/{client_slug(client_name)}.svg"


# Méthodes qui modifient l'état : soumises au contrôle d'origine (CSRF).
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Durée de vie d'une absence d'image mise en cache (fichier vide).
NEGATIVE_IMAGE_CACHE_SECONDS = 24 * 3600
# Identifiants Jellyfin (GUID avec ou sans tirets) — borne aussi les noms de
# fichiers du cache d'images.
JELLYFIN_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
BRANDING_EXTS = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".ico", ".gif"}
BRANDING_MAX_BYTES = 2 * 1024 * 1024
IMPORT_UPLOAD_MAX_BYTES = 512 * 1024 * 1024  # backups .tsv/.db volumineux
IMAGE_CACHE_HEADERS = {"Cache-Control": "public, max-age=86400"}
# Logo et favicon téléversés par un admin : un SVG ouvert directement dans le
# navigateur s'exécuterait sur l'origine de l'appli. La CSP interdit tout
# script (styles inline et images data: autorisés pour le rendu du SVG).
BRANDING_HEADERS = {
    **IMAGE_CACHE_HEADERS,
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
    "X-Content-Type-Options": "nosniff",
}


# Avatar par défaut : initiale sur une pastille de couleur stable par
# utilisateur (teintes lisibles sur le thème sombre, texte blanc).
AVATAR_COLORS = (
    "#775bf4",
    "#0b8fc4",
    "#9b4fb5",
    "#2e8f5e",
    "#c2721a",
    "#c8474e",
    "#3f6fd0",
    "#b23f73",
    "#0f8a7f",
    "#7a6a2e",
)


def initial_avatar_svg(name: str | None, key: str) -> str:
    """SVG rond avec la première lettre (ou chiffre) de ``name`` ; la couleur
    dépend de ``key`` (identifiant), pas du nom, pour rester stable."""
    initial = next((c for c in (name or "") if c.isalnum()), "?").upper()
    color = AVATAR_COLORS[zlib.crc32(key.encode()) % len(AVATAR_COLORS)]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<circle cx="50" cy="50" r="50" fill="{color}"/>'
        '<text x="50" y="50" dy=".35em" text-anchor="middle" fill="#fff"'
        ' font-family="system-ui, -apple-system, Segoe UI, Roboto, sans-serif"'
        f' font-size="46" font-weight="600">{escape(initial)}</text></svg>'
    )


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
        return f"{hours}h\u00a0{minutes:02d}m"  # insécable : jamais coupé sur deux lignes
    return f"{minutes}m"


def _datetime_html(value) -> Markup | str:
    """Date lisible (« hier 22:31 ») avec la date exacte en infobulle."""
    if not value:
        return ""
    exact = escape(str(value)[:16])
    return Markup(
        f'<time datetime="{exact}" title="{exact}">{escape(datetime_label(value))}</time>'
    )


def _duration_long(seconds) -> str:
    """Durée lisible pour infobulles : « 2 h 15 », « 3 h », « 45 min »."""
    seconds = int(seconds or 0)
    hours, minutes = seconds // 3600, seconds % 3600 // 60
    if not hours:
        return f"{minutes}\u00a0min"
    return f"{hours}\u00a0h\u00a0{minutes:02d}" if minutes else f"{hours}\u00a0h"


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["duration_hm"] = _duration_hm
    templates.env.filters["dt"] = _datetime_html
    templates.env.filters["duration_long"] = _duration_long
    templates.env.filters["date_long"] = date_long_label
    templates.env.filters["media_icon"] = media_icon
    templates.env.filters["media_type_label"] = media_type_label
    templates.env.filters["collection_label"] = collection_label
    templates.env.filters["library_icon"] = library_icon
    templates.env.filters["client_logo"] = client_logo
    templates.env.globals["icon"] = icon
    return templates
