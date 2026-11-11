"""Constantes et utilitaires partagés par les routes (icônes, MIME, CSV, templates)."""

import re
from pathlib import Path

from fastapi.templating import Jinja2Templates

from ..labels import collection_label, media_type_label

BASE_DIR = Path(__file__).resolve().parents[2]  # racine du dépôt (jellyfin_stats/web/ -> ..)
TEMPLATES_DIR = BASE_DIR / "data" / "interfaces" / "default" / "templates"
STATIC_DIR = BASE_DIR / "data" / "interfaces" / "default" / "static"

MEDIA_TYPES = ["Movie", "Episode", "Audio", "MusicVideo", "AudioBook"]
PERIOD_DAYS = {"24h": 1, "7d": 7, "30d": 30, "12m": 365, "all": None}

MEDIA_ICONS = {
    "Movie": "🎬",
    "Episode": "📺",
    "Series": "📺",
    "Season": "📺",
    "Audio": "🎵",
    "MusicAlbum": "💿",
    "MusicVideo": "🎤",
    "AudioBook": "🎧",
}

CLIENT_ICONS = [  # mapping par sous-chaîne, premier qui matche
    (
        (
            "android tv",
            "androidtv",
            "apple tv",
            "tvos",
            "roku",
            "tizen",
            "webos",
            "samsung",
            "lg tv",
            "shield",
        ),
        "📺",
    ),
    (("web", "browser", "chrome", "firefox", "edge", "safari", "opera"), "🌐"),
    (("iphone", "ipad", "ios", "swiftfin"), "📱"),
    (("android", "findroid"), "🤖"),
    (("kodi", "infuse", "emby", "plex"), "🎦"),
    (("mpv", "vlc", "mediaplayer"), "🎞️"),
    (("dlna", "cast", "chromecast"), "📡"),
]


LIBRARY_ICONS = {
    "movies": "🎬",
    "tvshows": "📺",
    "music": "🎵",
    "musicvideos": "🎤",
    "books": "📚",
    "homevideos": "🎥",
    "photos": "🖼️",
    "boxsets": "🎞️",
    "livetv": "📡",
    "playlists": "🎼",
}


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
    templates.env.filters["media_type_label"] = media_type_label
    templates.env.filters["collection_label"] = collection_label
    templates.env.filters["library_icon"] = library_icon
    templates.env.filters["client_icon"] = client_icon
    templates.env.filters["client_logo"] = client_logo
    return templates
