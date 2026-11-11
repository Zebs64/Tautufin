"""En-têtes X-Forwarded-For / -Proto des reverse proxys de confiance.

La liste des proxys de confiance (``[Web] trusted_proxies``) est modifiable
depuis les Réglages. uvicorn ne la lit qu'au démarrage ; ce middleware applique
donc lui-même celui d'uvicorn, reconstruit dès que la valeur change, pour
qu'une modification prenne effet sans redémarrage.
"""

import ipaddress

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

DEFAULT_TRUSTED_PROXIES = "127.0.0.1"


def normalize_trusted_proxies(value: str) -> str:
    """Valide et normalise une liste d'IP ou de réseaux séparés par des
    virgules (ou « * » pour tout accepter). Lève ValueError avec un message
    lisible : uvicorn, lui, accepte en silence une entrée mal formée."""
    entries = [e.strip() for e in (value or "").replace(";", ",").split(",") if e.strip()]
    if not entries:
        return DEFAULT_TRUSTED_PROXIES
    if "*" in entries:
        if len(entries) > 1:
            raise ValueError("« * » (tous les proxys) ne se combine pas avec d'autres entrées.")
        return "*"
    normalized = []
    for entry in entries:
        try:
            if "/" in entry:
                normalized.append(str(ipaddress.ip_network(entry, strict=False)))
            else:
                normalized.append(str(ipaddress.ip_address(entry)))
        except ValueError:
            raise ValueError(f"Adresse IP ou réseau invalide : « {entry} ».") from None
    return ", ".join(dict.fromkeys(normalized))


class DynamicProxyHeaders:
    """ProxyHeadersMiddleware d'uvicorn, avec une liste de confiance relue à
    chaque requête (reconstruction seulement quand elle change)."""

    def __init__(self, app, get_trusted_proxies):
        self.app = app
        self.get_trusted_proxies = get_trusted_proxies
        self._trusted = None
        self._middleware = None

    async def __call__(self, scope, receive, send):
        trusted = self.get_trusted_proxies()
        if trusted != self._trusted:
            self._middleware = ProxyHeadersMiddleware(self.app, trusted_hosts=trusted)
            self._trusted = trusted
        await self._middleware(scope, receive, send)
