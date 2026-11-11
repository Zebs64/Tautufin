"""Libellés français des valeurs brutes de Jellyfin (types de média, types de
bibliothèque, méthodes de lecture). Les valeurs brutes restent les clés en
base, dans les filtres d'URL et dans l'export CSV ; seul l'affichage change."""

import re

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
