"""Gestion de la configuration (config.ini)."""

import configparser
import logging
import os
import secrets
import threading

logger = logging.getLogger(__name__)

DEFAULTS = {
    "Web": {"host": "0.0.0.0", "port": "8181"},
    "Jellyfin": {"url": "", "api_key": "", "verify_ssl": "true"},
    "Auth": {
        "session_lifetime": "604800",
        "jellyfin_auth_enabled": "true",
        "local_auth_enabled": "true",
        "secret_key": "",
    },
    "Monitoring": {
        "minimum_duration": "300",
        "minimum_percent": "0",
        "poll_interval": "15",
        "sync_interval": "3600",
        "session_grace": "90",
    },
    "Clients": {
        # Historique importé sans client (ex. backup Plex synchronisé) :
        # le traiter comme « Plex » et/ou le masquer des stats clients.
        "unknown_as_plex": "false",
        "hide_unknown_clients": "true",
    },
    "UI": {"allow_user_library_pages": "true"},
    "Energy": {
        # Base de l'estimation du coût électrique du transcodage (page graphs) :
        # surconsommation supposée pendant un transcodage vidéo et prix du kWh.
        "transcode_watts": "50",
        "electricity_price": "0.27",
    },
    "Database": {"path": "data/jellyfin_stats.db"},
}


class Config:
    """Wrapper configparser : lecture avec défauts, écriture atomique."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        self._parser = configparser.ConfigParser()
        if os.path.exists(self.path):
            self._parser.read(self.path, encoding="utf-8")
        for section, keys in DEFAULTS.items():
            if not self._parser.has_section(section):
                self._parser.add_section(section)
            for key, value in keys.items():
                if not self._parser.has_option(section, key):
                    self._parser.set(section, key, value)
        if not self.secret_key:
            self.set("Auth", "secret_key", secrets.token_urlsafe(48))
            logger.info("Secret de session généré et enregistré dans %s", self.path)
        self.save()

    def get(self, section: str, key: str) -> str:
        return self._parser.get(section, key, fallback=DEFAULTS[section][key])

    def get_int(self, section: str, key: str) -> int:
        try:
            return int(self.get(section, key) or DEFAULTS[section][key])
        except ValueError:
            return int(DEFAULTS[section][key])

    def get_float(self, section: str, key: str) -> float:
        try:
            return float(self.get(section, key) or DEFAULTS[section][key])
        except ValueError:
            return float(DEFAULTS[section][key])

    def get_bool(self, section: str, key: str) -> bool:
        return self.get(section, key).strip().lower() in ("1", "true", "yes", "on")

    def set(self, section: str, key: str, value) -> None:
        self._parser.set(section, key, str(value))

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                self._parser.write(f)
            os.replace(tmp, self.path)

    # --- Raccourcis typés -------------------------------------------------

    @property
    def jellyfin_url(self) -> str:
        return self.get("Jellyfin", "url").rstrip("/")

    @property
    def jellyfin_api_key(self) -> str:
        return self.get("Jellyfin", "api_key")

    @property
    def verify_ssl(self) -> bool:
        return self.get_bool("Jellyfin", "verify_ssl")

    @property
    def jellyfin_configured(self) -> bool:
        return bool(self.jellyfin_url and self.jellyfin_api_key)

    @property
    def secret_key(self) -> str:
        return self.get("Auth", "secret_key")

    @property
    def session_lifetime(self) -> int:
        return self.get_int("Auth", "session_lifetime")

    @property
    def jellyfin_auth_enabled(self) -> bool:
        return self.get_bool("Auth", "jellyfin_auth_enabled")

    @property
    def local_auth_enabled(self) -> bool:
        return self.get_bool("Auth", "local_auth_enabled")

    @property
    def minimum_duration(self) -> int:
        return self.get_int("Monitoring", "minimum_duration")

    @property
    def minimum_percent(self) -> int:
        return self.get_int("Monitoring", "minimum_percent")

    @property
    def poll_interval(self) -> int:
        return max(5, self.get_int("Monitoring", "poll_interval"))

    @property
    def sync_interval(self) -> int:
        return max(60, self.get_int("Monitoring", "sync_interval"))

    @property
    def session_grace(self) -> int:
        """Délai (s) d'absence du polling avant de finaliser une session, pour
        ne pas fragmenter une lecture continue sur un trou transitoire."""
        return max(0, self.get_int("Monitoring", "session_grace"))

    @property
    def unknown_as_plex(self) -> bool:
        """Afficher les lectures sans client connu comme provenant de « Plex »."""
        return self.get_bool("Clients", "unknown_as_plex")

    @property
    def hide_unknown_clients(self) -> bool:
        """Exclure les clients inconnus des graphes clients (accueil + graphs).
        La page utilisateur les conserve toujours."""
        return self.get_bool("Clients", "hide_unknown_clients")

    @property
    def allow_user_library_pages(self) -> bool:
        return self.get_bool("UI", "allow_user_library_pages")

    @property
    def transcode_watts(self) -> int:
        return max(0, self.get_int("Energy", "transcode_watts"))

    @property
    def electricity_price(self) -> float:
        return max(0.0, self.get_float("Energy", "electricity_price"))

    @property
    def database_path(self) -> str:
        path = self.get("Database", "path")
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(self.path), path)
        return path
