"""Cache disque des images servies par le proxy (posters, avatars).

Une entrée par média et par largeur demandée ; sans limite, le dossier grossit
indéfiniment. ``trim`` (boucle de nettoyage horaire) le ramène sous
``MAX_BYTES`` en supprimant les fichiers les plus anciennement écrits, et
``clear`` le vide sur demande (Réglages > Maintenance).
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_BYTES = 500 * 1024 * 1024


def cache_dir(config) -> Path:
    return Path(config.path).parent / "cache" / "images"


def _entries(directory: Path) -> list[tuple[float, int, Path]]:
    """(date d'écriture, taille, chemin) des fichiers du cache ; un fichier
    disparu entre-temps (requête concurrente) est ignoré."""
    entries = []
    if not directory.is_dir():
        return entries
    for path in directory.iterdir():
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if path.is_file():
            entries.append((stat.st_mtime, stat.st_size, path))
    return entries


def trim(directory: Path, max_bytes: int = MAX_BYTES) -> int:
    """Supprime les fichiers les plus anciens jusqu'à repasser sous
    ``max_bytes``. Renvoie le nombre de fichiers supprimés."""
    entries = sorted(_entries(directory))
    total = sum(size for _, size, _ in entries)
    removed = 0
    for _, size, path in entries:
        if total <= max_bytes:
            break
        path.unlink(missing_ok=True)
        total -= size
        removed += 1
    if removed:
        logger.info("Cache d'images : %d fichier(s) ancien(s) supprimé(s)", removed)
    return removed


def clear(directory: Path) -> dict:
    """Vide le cache. Renvoie le nombre de fichiers et d'octets libérés."""
    entries = _entries(directory)
    for _, _, path in entries:
        path.unlink(missing_ok=True)
    return {"files": len(entries), "bytes": sum(size for _, size, _ in entries)}
