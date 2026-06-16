"""Authentification : login Jellyfin + comptes locaux, sessions serveur.

- Les sessions vivent côté serveur (table ``http_sessions``) ; le cookie ne
  contient que le token de session, signé avec ``itsdangerous``.
- Le token Jellyfin obtenu au login n'est jamais stocké : seuls l'UserId,
  le nom et le rôle sont persistés dans la session.
- Rate limiting du login : 5 tentatives / minute / IP, en mémoire.
"""

import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

import bcrypt
from itsdangerous import BadSignature, URLSafeSerializer

from . import database
from .database import now_iso

logger = logging.getLogger(__name__)

SESSION_COOKIE = "jfstats_session"

LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 60


class AccessBlocked(Exception):
    """L'utilisateur Jellyfin est explicitement bloqué dans Tautufin."""


@dataclass
class CurrentUser:
    token: str
    auth_mode: str               # 'jellyfin' | 'local'
    username: str
    role: str                    # 'admin' | 'user'
    jellyfin_user_id: str | None # None pour un compte local non lié
    can_view_all: bool = False   # droit « vision » (résolu en direct)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_view_everyone(self) -> bool:
        """Voit les stats de tout le monde : admin ou droit « vision »."""
        return self.is_admin or self.can_view_all


class RateLimiter:
    def __init__(self, max_attempts=LOGIN_MAX_ATTEMPTS, window=LOGIN_WINDOW_SECONDS):
        self.max_attempts = max_attempts
        self.window = window
        self._attempts: dict[str, deque] = {}
        self._lock = threading.Lock()

    def check(self, ip: str) -> bool:
        """Enregistre une tentative ; False si la limite est dépassée."""
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts.setdefault(ip, deque())
            while attempts and now - attempts[0] > self.window:
                attempts.popleft()
            if len(attempts) >= self.max_attempts:
                return False
            attempts.append(now)
            return True


# --- Mots de passe locaux ---------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def check_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


# --- Comptes locaux ---------------------------------------------------------

def create_local_user(username: str, password: str, role: str = "user",
                      jellyfin_user_id: str | None = None) -> None:
    database.execute(
        "INSERT INTO local_users (username, password_hash, role, jellyfin_user_id)"
        " VALUES (?, ?, ?, ?)",
        (username, hash_password(password), role, jellyfin_user_id or None),
    )

def get_local_user(username: str) -> dict | None:
    return database.query_one(
        "SELECT * FROM local_users WHERE username = ?", (username,)
    )

def list_local_users() -> list[dict]:
    return database.query(
        "SELECT id, username, role, jellyfin_user_id, created_at, last_login"
        " FROM local_users ORDER BY username"
    )

def delete_local_user(user_id: int) -> None:
    database.execute("DELETE FROM local_users WHERE id = ?", (user_id,))

def set_local_password(username: str, password: str) -> bool:
    return database.execute(
        "UPDATE local_users SET password_hash = ? WHERE username = ?",
        (hash_password(password), username),
    ) > 0

def has_local_admin() -> bool:
    return database.query_one(
        "SELECT 1 FROM local_users WHERE role = 'admin' LIMIT 1"
    ) is not None


# --- Logins -----------------------------------------------------------------

def login_jellyfin(api, username: str, password: str) -> CurrentUser | None:
    """Authentifie via POST /Users/AuthenticateByName. Le token retourné par
    Jellyfin est ignoré après cet appel (jamais stocké)."""
    result = api.authenticate_by_name(username, password)
    if not result or "User" not in result:
        return None
    jf_user = result["User"]
    is_admin = bool(jf_user.get("Policy", {}).get("IsAdministrator"))
    # Met à jour le cache local des utilisateurs Jellyfin.
    with database.db() as conn:
        conn.execute(
            """
            INSERT INTO users (jellyfin_user_id, username, is_admin, last_activity)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(jellyfin_user_id) DO UPDATE SET
                username = excluded.username,
                is_admin = excluded.is_admin,
                is_active = 1,
                last_activity = excluded.last_activity
            """,
            (jf_user["Id"], jf_user.get("Name", username), int(is_admin), now_iso()),
        )
    # Accès à Tautufin révoqué par un admin (indépendant du compte Jellyfin).
    blocked = database.query_one(
        "SELECT access_blocked FROM users WHERE jellyfin_user_id = ?", (jf_user["Id"],)
    )
    if blocked and blocked["access_blocked"]:
        raise AccessBlocked()
    return _create_session(
        auth_mode="jellyfin",
        username=jf_user.get("Name", username),
        role="admin" if is_admin else "user",
        jellyfin_user_id=jf_user["Id"],
    )


def login_local(username: str, password: str) -> CurrentUser | None:
    user = get_local_user(username)
    if not user or not check_password(password, user["password_hash"]):
        return None
    database.execute(
        "UPDATE local_users SET last_login = ? WHERE id = ?", (now_iso(), user["id"])
    )
    return _create_session(
        auth_mode="local",
        username=user["username"],
        role=user["role"],
        jellyfin_user_id=user["jellyfin_user_id"],
    )


# --- Sessions HTTP ------------------------------------------------------------

_session_lifetime = 604800
_serializer: URLSafeSerializer | None = None


def init(secret_key: str, session_lifetime: int) -> None:
    global _serializer, _session_lifetime
    _serializer = URLSafeSerializer(secret_key, salt="jfstats-session")
    _session_lifetime = session_lifetime


def _create_session(auth_mode, username, role, jellyfin_user_id) -> CurrentUser:
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires = now + timedelta(seconds=_session_lifetime)
    database.execute(
        "INSERT INTO http_sessions"
        " (token, auth_mode, username, role, jellyfin_user_id,"
        "  created_at, expires_at, last_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (token, auth_mode, username, role, jellyfin_user_id,
         now.strftime("%Y-%m-%d %H:%M:%S"), expires.strftime("%Y-%m-%d %H:%M:%S"),
         now.strftime("%Y-%m-%d %H:%M:%S")),
    )
    return CurrentUser(token, auth_mode, username, role, jellyfin_user_id)


def cookie_value(user: CurrentUser) -> str:
    return _serializer.dumps(user.token)


def resolve_session(cookie: str | None) -> CurrentUser | None:
    """Cookie signé → session serveur valide → CurrentUser, sinon None."""
    if not cookie or _serializer is None:
        return None
    try:
        token = _serializer.loads(cookie)
    except BadSignature:
        return None
    row = database.query_one("SELECT * FROM http_sessions WHERE token = ?", (token,))
    if not row:
        return None
    if row["expires_at"] < now_iso():
        database.execute("DELETE FROM http_sessions WHERE token = ?", (token,))
        return None
    # Droits gérés par Tautufin (blocage / vision) résolus en direct, pour que
    # toute modification admin prenne effet immédiatement (sans re-login).
    can_view_all = False
    if row["jellyfin_user_id"]:
        flags = database.query_one(
            "SELECT access_blocked, can_view_all FROM users WHERE jellyfin_user_id = ?",
            (row["jellyfin_user_id"],),
        )
        if flags:
            if flags["access_blocked"] and row["auth_mode"] == "jellyfin":
                database.execute("DELETE FROM http_sessions WHERE token = ?", (token,))
                return None
            can_view_all = bool(flags["can_view_all"])
    database.execute(
        "UPDATE http_sessions SET last_active = ? WHERE token = ?", (now_iso(), token)
    )
    return CurrentUser(
        token=row["token"],
        auth_mode=row["auth_mode"],
        username=row["username"],
        role=row["role"],
        jellyfin_user_id=row["jellyfin_user_id"],
        can_view_all=can_view_all,
    )


def destroy_session(token: str) -> None:
    database.execute("DELETE FROM http_sessions WHERE token = ?", (token,))


def purge_expired_sessions() -> int:
    return database.execute(
        "DELETE FROM http_sessions WHERE expires_at < ?", (now_iso(),)
    )
