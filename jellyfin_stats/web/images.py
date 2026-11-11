"""Proxy d'images Jellyfin (posters, avatars) avec cache disque."""

import time

from fastapi import Depends, FastAPI, Query
from fastapi.responses import (
    FileResponse,
    Response,
)

from .. import (
    database,
)
from ..auth import CurrentUser
from .helpers import (
    IMAGE_CACHE_HEADERS,
    JELLYFIN_ID_RE,
    NEGATIVE_IMAGE_CACHE_SECONDS,
    STATIC_DIR,
    _sniff_mime,
)


def register(app: FastAPI, context) -> None:
    config = context.config
    api = context.api
    image_cache_dir = context.image_cache_dir
    require_user = context.require_user

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
            data = cache_file.read_bytes()
            # Un négatif (fichier vide) expire : l'image a pu être ajoutée
            # dans Jellyfin depuis (affiche récupérée après l'ajout du média).
            age = time.time() - cache_file.stat().st_mtime
            if data or age < NEGATIVE_IMAGE_CACHE_SECONDS:
                return data or None
        result = fetch() if config.jellyfin_configured else None
        if result:
            cache_file.write_bytes(result[0])
            return result[0]
        if config.jellyfin_configured:  # négatif seulement si réponse réelle
            cache_file.write_bytes(b"")
        return None

    def _image_response(data: bytes | None, placeholder: str) -> Response:
        if data:
            return Response(data, media_type=_sniff_mime(data), headers=IMAGE_CACHE_HEADERS)
        return FileResponse(STATIC_DIR / "img" / placeholder, headers=IMAGE_CACHE_HEADERS)

    @app.get("/image/item/{item_id}")
    def image_item(
        item_id: str,
        fallback: str | None = Query(None),
        w: int = Query(480, ge=40, le=1200),
        type: str = Query("Primary"),
        user: CurrentUser = Depends(require_user),
    ):
        # Backdrop (fond « hero » des cartes du dashboard) : on tente l'art
        # paysage du média ; s'il est absent on retombe sur le poster.
        if type == "Backdrop" and JELLYFIN_ID_RE.match(item_id):
            data = _cached_fetch(
                f"item_{item_id}_bd_{w}",
                lambda: api.get_image(item_id, image_type="Backdrop", max_width=w),
            )
            if data is not None:
                return _image_response(data, "poster-placeholder.svg")
        data = None
        if JELLYFIN_ID_RE.match(item_id):
            data = _cached_fetch(f"item_{item_id}_{w}", lambda: api.get_image(item_id, max_width=w))
        if data is None and fallback and JELLYFIN_ID_RE.match(fallback):
            data = _cached_fetch(
                f"item_{fallback}_{w}", lambda: api.get_image(fallback, max_width=w)
            )
        if data is None and not fallback and JELLYFIN_ID_RE.match(item_id):
            # Épisode sans image propre : tenter le poster de sa série.
            series = database.query_one(
                """
                SELECT COALESCE(
                    e.series_id,
                    (SELECT s.item_id FROM items s
                     WHERE s.type = 'Series' AND s.name = e.series_name LIMIT 1)
                ) AS id
                FROM items e WHERE e.item_id = ?
                """,
                (item_id,),
            )
            if series and series["id"]:
                data = _cached_fetch(
                    f"item_{series['id']}_{w}", lambda: api.get_image(series["id"], max_width=w)
                )
        return _image_response(data, "poster-placeholder.svg")

    @app.get("/image/user/{user_id}")
    def image_user(user_id: str, user: CurrentUser = Depends(require_user)):
        data = None
        if JELLYFIN_ID_RE.match(user_id):
            data = _cached_fetch(f"user_{user_id}", lambda: api.get_user_image(user_id))
        return _image_response(data, "avatar-placeholder.svg")
