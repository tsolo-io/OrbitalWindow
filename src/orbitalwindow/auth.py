# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Passwords, the ``ow_session`` cookie and the login rate limit."""

import os
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .db import Database

COOKIE_NAME = "ow_session"

_hasher = PasswordHasher()
# Verified against when the user does not exist, so a miss costs the same time as a hit.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    """Hash a password with argon2.

    Args:
        password: The plain-text password.

    Returns:
        The encoded argon2 hash.
    """
    return _hasher.hash(password)


def verify_password(pw_hash: str, password: str) -> bool:
    """Check a password against a stored hash.

    Args:
        pw_hash: The stored argon2 hash.
        password: The plain-text password to check.

    Returns:
        True if the password matches; False if it does not or the hash is invalid.
    """
    try:
        return _hasher.verify(pw_hash, password)
    except VerificationError, InvalidHashError:
        return False


def needs_rehash(pw_hash: str) -> bool:
    """Tell whether a hash was made with outdated argon2 parameters.

    Args:
        pw_hash: The stored argon2 hash.

    Returns:
        True if the hash should be recomputed with the current parameters.
    """
    return _hasher.check_needs_rehash(pw_hash)


def generate_password() -> str:
    """Return a password that is easy to read out and type: four groups of letters/digits.

    Returns:
        A password like ``abcd-ef23-ghjk-mn45``.
    """
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))


def load_or_create_secret(path: Path) -> bytes:
    """Read the session signing key, creating it (mode 600) on first start.

    Args:
        path: Location of the key file.

    Returns:
        The key bytes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        pass
    key = secrets.token_bytes(64)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


@dataclass(frozen=True)
class SessionUser:
    id: int
    name: str
    display_name: str
    is_admin: bool
    is_ai_manager: bool = False

    @property
    def can_manage_ai(self) -> bool:
        """Tell whether the user may manage the AI functions (AI managers and admins).

        Returns:
            True for AI managers and administrators.
        """
        return self.is_admin or self.is_ai_manager


class Sessions:
    """Signed, time-limited session tokens checked against ``users.session_ver`` on every use."""

    def __init__(self, db: Database, secret: bytes, max_age_seconds: int) -> None:
        """Create the session token handler.

        Args:
            db: The application database.
            secret: Key used to sign tokens.
            max_age_seconds: How long a token stays valid.
        """
        self._db = db
        self._serializer = URLSafeTimedSerializer(secret, salt="ow-session")
        self.max_age = max_age_seconds

    def issue(self, user_id: int, session_ver: int) -> str:
        """Create a signed session token.

        Args:
            user_id: The user's database id.
            session_ver: The user's current session version.

        Returns:
            The signed token.
        """
        return self._serializer.dumps({"u": user_id, "v": session_ver})

    def resolve(self, token: str | None) -> SessionUser | None:
        """Return the user a token belongs to, if the token is still valid.

        Args:
            token: The session token from the cookie.

        Returns:
            The session user, or None if the token is missing, invalid, expired, its session
            version is outdated or the account is disabled.
        """
        if not token:
            return None
        try:
            data = self._serializer.loads(token, max_age=self.max_age)
            user_id, ver = int(data["u"]), int(data["v"])
        except BadSignature, KeyError, TypeError, ValueError:
            return None
        row = self._db.one(
            "SELECT id, name, display_name, is_admin, coalesce(is_ai_manager, false) AS ai, "
            "disabled, session_ver FROM users WHERE id = ?",
            [user_id],
        )
        if row is None or row["disabled"] or row["session_ver"] != ver:
            return None
        return SessionUser(
            row["id"], row["name"], row["display_name"], row["is_admin"], bool(row["ai"])
        )


class LoginRateLimiter:
    """Refuses logins after too many recent failures, per client address and per user name."""

    def __init__(self, max_failures: int, window_seconds: int) -> None:
        """Create the rate limiter.

        Args:
            max_failures: Failures within the window after which a key is blocked.
            window_seconds: How long a failure counts.
        """
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        """Return a key's failure times, dropping those older than the window.

        Args:
            key: The limiter key (e.g. ``ip:…`` or ``user:…``).
            now: The current monotonic time.

        Returns:
            The (live) deque of recent failure times for the key.
        """
        q = self._failures[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def _prune(self, now: float) -> None:
        """Forget keys with no recent failures, so random names don't pile up in memory.

        Args:
            now: The current monotonic time.
        """
        for key in [k for k, q in self._failures.items() if not q or q[-1] <= now - self.window]:
            del self._failures[key]

    def blocked(self, *keys: str) -> bool:
        """Tell whether any of the keys has too many recent failures.

        Args:
            *keys: The limiter keys to check.

        Returns:
            True if at least one key is blocked.
        """
        now = time.monotonic()
        with self._lock:
            return any(len(self._recent(k, now)) >= self.max_failures for k in keys)

    def failure(self, *keys: str) -> None:
        """Record a failed attempt for each key.

        Args:
            *keys: The limiter keys to charge.
        """
        now = time.monotonic()
        with self._lock:
            if len(self._failures) > 1000:
                self._prune(now)
            for key in keys:
                self._recent(key, now).append(now)

    def success(self, *keys: str) -> None:
        """Clear the recorded failures of each key.

        Args:
            *keys: The limiter keys to reset.
        """
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)


def authenticate(db: Database, name: str, password: str) -> dict | None:
    """Return the user row if the name and password match and the account is enabled.

    Upgrades the stored hash when it was made with outdated parameters.

    Args:
        db: The application database.
        name: The user name (case and surrounding spaces are ignored).
        password: The plain-text password.

    Returns:
        The user row, or None if the login is not valid.
    """
    row = db.one("SELECT * FROM users WHERE name = ?", [name.strip().lower()])
    if row is None:
        verify_password(_DUMMY_HASH, password)
        return None
    if not verify_password(row["pw_hash"], password) or row["disabled"]:
        return None
    if needs_rehash(row["pw_hash"]):
        db.execute(
            "UPDATE users SET pw_hash = ? WHERE id = ?", [hash_password(password), row["id"]]
        )
    return row
