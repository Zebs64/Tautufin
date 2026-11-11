"""Libellés français des valeurs brutes de Jellyfin (types de média, types de
bibliothèque, méthodes de lecture) et dates lisibles. Les valeurs brutes
restent les clés en base, dans les filtres d'URL et dans l'export CSV ; seul
l'affichage change. Les règles de dates sont dupliquées dans app.js."""

import re
from datetime import date, datetime, timedelta

MEDIA_TYPE_LABELS = {
    "Movie": "Film",
    "Episode": "Épisode",
    "Series": "Série",
    "Season": "Saison",
    "Audio": "Musique",
    "MusicAlbum": "Album",
    "MusicVideo": "Clip",
    "AudioBook": "Livre audio",
    "BoxSet": "Collection",
}

COLLECTION_LABELS = {
    "movies": "Films",
    "tvshows": "Séries",
    "music": "Musique",
    "musicvideos": "Clips",
    "books": "Livres",
    "homevideos": "Vidéos personnelles",
    "photos": "Photos",
    "boxsets": "Collections",
    "livetv": "TV en direct",
    "playlists": "Listes de lecture",
    "mixed": "Mixte",
}

PLAY_METHOD_LABELS = {
    "DirectPlay": "Lecture directe",
    "DirectStream": "Remux direct",
    "Transcode": "Transcodage",
}

# Format Playback Reporting : « Transcode (v:h264 a:direct) ».
_TRANSCODE_DETAIL = re.compile(r"^Transcode \(v:(\w+) a:(\w+)\)$")


def media_type_label(value: str | None) -> str:
    if not value:
        return "Inconnu"
    return MEDIA_TYPE_LABELS.get(value, value)


def collection_label(value: str | None) -> str:
    if not value:
        return "Bibliothèque"
    return COLLECTION_LABELS.get(value, value)


def play_method_label(value: str | None) -> str:
    if not value:
        return "Inconnu"
    match = _TRANSCODE_DETAIL.match(value)
    if match:
        video, audio = match.groups()
        video = "vidéo directe" if video == "direct" else f"vidéo {video.upper()}"
        audio = "audio direct" if audio == "direct" else f"audio {audio.upper()}"
        return f"Transcodage ({video}, {audio})"
    return PLAY_METHOD_LABELS.get(value, value)


# --- Dates ------------------------------------------------------------------

MONTHS_SHORT = [
    "janv.", "févr.", "mars", "avr.", "mai", "juin",
    "juil.", "août", "sept.", "oct.", "nov.", "déc.",
]  # fmt: skip
MONTHS_LONG = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]  # fmt: skip


def _parse(value) -> datetime | None:
    """Horodatage stocké (« AAAA-MM-JJ HH:MM:SS », heure locale) ou date seule."""
    try:
        return datetime.fromisoformat(str(value).strip()[:19].replace("T", " "))
    except (TypeError, ValueError):
        return None


def _day(d: date) -> str:
    return "1er" if d.day == 1 else str(d.day)


def datetime_label(value, today: date | None = None) -> str:
    """« aujourd'hui 22:31 », « hier 22:31 », « 10 nov. 22:31 » (année en cours)
    ou « 10 nov. 2025 22:31 ». Valeur illisible : rendue telle quelle."""
    parsed = _parse(value)
    if parsed is None:
        return str(value or "")
    today = today or date.today()
    day, time = parsed.date(), parsed.strftime("%H:%M")
    if day == today:
        return f"aujourd'hui {time}"
    if day == today - timedelta(days=1):
        return f"hier {time}"
    label = f"{_day(day)} {MONTHS_SHORT[day.month - 1]}"
    if day.year != today.year:
        label += f" {day.year}"
    return f"{label} {time}"


def date_long_label(value) -> str:
    """« 10 novembre 2025 », « 1er août 2018 »."""
    parsed = _parse(value)
    if parsed is None:
        return str(value or "")
    day = parsed.date()
    return f"{_day(day)} {MONTHS_LONG[day.month - 1]} {day.year}"
