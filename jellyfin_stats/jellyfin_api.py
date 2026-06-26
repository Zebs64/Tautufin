"""Wrapper de l'API REST Jellyfin.

Endpoints utilisés (référence : https://api.jellyfin.org) :
- POST /Users/AuthenticateByName  — login par identifiants Jellyfin
- GET  /System/Info               — test de connexion
- GET  /Users, /Users/{id}        — liste et détail des utilisateurs
- GET  /Sessions                  — sessions de lecture actives
- GET  /Library/VirtualFolders    — bibliothèques
- GET  /Items                     — médias (paginé)

Authentification : header standard ``Authorization`` portant une valeur
``MediaBrowser ...`` avec la clé API (``Token="..."``). Le token utilisateur
obtenu au login n'est utilisé que le temps de la requête de login, jamais
persisté.
"""

import logging

import httpx

from . import __version__

logger = logging.getLogger(__name__)

CLIENT_NAME = "JellyfinStats"
DEVICE_ID = "jellyfin-stats-server"


class JellyfinError(Exception):
    pass


class JellyfinAPI:
    def __init__(self, config):
        self.config = config

    # --- Bas niveau -------------------------------------------------------

    def _auth_header(self, token: str | None = None) -> str:
        parts = (
            f'MediaBrowser Client="{CLIENT_NAME}", Device="{CLIENT_NAME}", '
            f'DeviceId="{DEVICE_ID}", Version="{__version__}"'
        )
        if token:
            parts += f', Token="{token}"'
        return parts

    def _auth_headers(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": self._auth_header(token)}

    def _request(
        self,
        method: str,
        path: str,
        token: str | None = None,
        base_url: str | None = None,
        **kwargs,
    ):
        base = (base_url or self.config.jellyfin_url).rstrip("/")
        if not base:
            raise JellyfinError("Serveur Jellyfin non configuré")
        headers = self._auth_headers(token)
        try:
            resp = httpx.request(
                method,
                f"{base}{path}",
                headers=headers,
                verify=self.config.verify_ssl,
                timeout=30,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise JellyfinError(f"Jellyfin injoignable : {exc}") from exc
        if resp.status_code in (401, 403):
            raise JellyfinError(f"Accès refusé par Jellyfin (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise JellyfinError(f"Erreur Jellyfin HTTP {resp.status_code} sur {path}")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _get(self, path: str, params: dict | None = None):
        return self._request("GET", path, token=self.config.jellyfin_api_key, params=params)

    # --- Authentification -------------------------------------------------

    def authenticate_by_name(self, username: str, password: str) -> dict | None:
        """Retourne la réponse Jellyfin (User + token) ou None si refusé."""
        try:
            return self._request(
                "POST",
                "/Users/AuthenticateByName",
                json={"Username": username, "Pw": password},
            )
        except JellyfinError as exc:
            logger.info("Login Jellyfin refusé pour %r : %s", username, exc)
            return None

    # --- Lecture ------------------------------------------------------------

    def ping(self, base_url: str | None = None, api_key: str | None = None) -> dict:
        """Test de connexion ; lève JellyfinError en cas d'échec."""
        return self._request(
            "GET",
            "/System/Info",
            token=api_key if api_key is not None else self.config.jellyfin_api_key,
            base_url=base_url,
        )

    def get_users(self) -> list[dict]:
        return self._get("/Users") or []

    def get_user(self, user_id: str) -> dict | None:
        try:
            return self._get(f"/Users/{user_id}")
        except JellyfinError:
            return None

    def get_sessions(self) -> list[dict]:
        return self._get("/Sessions") or []

    def get_libraries(self) -> list[dict]:
        return self._get("/Library/VirtualFolders") or []

    # --- Images ---------------------------------------------------------

    def get_image(self, item_id: str, image_type: str = "Primary",
                  max_width: int = 480) -> tuple[bytes, str] | None:
        """Poster/artwork d'un média ; None si absent ou serveur injoignable."""
        return self._get_binary(f"/Items/{item_id}/Images/{image_type}",
                                {"maxWidth": max_width, "quality": 90})

    def get_user_image(self, user_id: str,
                       max_width: int = 128) -> tuple[bytes, str] | None:
        """Avatar d'un utilisateur Jellyfin ; None si absent."""
        return self._get_binary("/UserImage",
                                {"userId": user_id, "maxWidth": max_width}) \
            or self._get_binary(f"/Users/{user_id}/Images/Primary",
                                {"maxWidth": max_width})

    def _get_binary(self, path: str, params: dict) -> tuple[bytes, str] | None:
        base = self.config.jellyfin_url
        if not base:
            return None
        try:
            resp = httpx.get(
                f"{base}{path}",
                params=params,
                headers=self._auth_headers(self.config.jellyfin_api_key),
                verify=self.config.verify_ssl,
                timeout=15,
            )
        except httpx.HTTPError:
            return None
        if resp.status_code != 200 or not resp.content:
            return None
        return resp.content, resp.headers.get("Content-Type", "image/jpeg")

    def iter_items(self, parent_id: str, page_size: int = 500):
        """Itère sur les médias d'une bibliothèque, avec pagination."""
        start = 0
        while True:
            data = self._get(
                "/Items",
                params={
                    "ParentId": parent_id,
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie,Series,Episode,Audio,MusicAlbum",
                    "Fields": "Genres,RunTimeTicks,ProductionYear,MediaStreams,"
                              "SeriesName,ParentIndexNumber,IndexNumber,DateCreated,"
                              "People",
                    "StartIndex": start,
                    "Limit": page_size,
                },
            ) or {}
            items = data.get("Items", [])
            yield from items
            start += len(items)
            if not items or start >= data.get("TotalRecordCount", 0):
                return

    def iter_played_items(self, user_id: str,
                          item_types: str = "Movie,Episode,Audio,AudioBook,MusicVideo",
                          page_size: int = 500):
        """Itère sur les médias marqués « Lu » par un utilisateur.

        Filtre ``IsPlayed=true`` côté Jellyfin ; chaque item porte un champ
        ``UserData`` (``Played``, ``LastPlayedDate``, ``PlayCount``) qui permet
        d'inférer une session de lecture pour les visionnages jamais capturés
        en direct (cf. infer_history)."""
        start = 0
        while True:
            data = self._get(
                "/Items",
                params={
                    "UserId": user_id,
                    "Recursive": "true",
                    "IsPlayed": "true",
                    "IsFolder": "false",
                    "IncludeItemTypes": item_types,
                    "Fields": "UserData,Genres,RunTimeTicks,ProductionYear,"
                              "MediaStreams,SeriesName,SeriesId,"
                              "ParentIndexNumber,IndexNumber",
                    "StartIndex": start,
                    "Limit": page_size,
                },
            ) or {}
            items = data.get("Items", [])
            yield from items
            start += len(items)
            if not items or start >= data.get("TotalRecordCount", 0):
                return
