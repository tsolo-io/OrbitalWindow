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
    return _hasher.hash(password)


def verify_password(pw_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(pw_hash, password)
    except VerificationError, InvalidHashError:
        return False


def needs_rehash(pw_hash: str) -> bool:
    return _hasher.check_needs_rehash(pw_hash)


def generate_password() -> str:
    """A password that is easy to read out and type: four groups of letters/digits."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))


def load_or_create_secret(path: Path) -> bytes:
    """Read the session signing key, creating it (mode 600) on first start."""
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


class Sessions:
    """Signed, time-limited session tokens checked against ``users.session_ver`` on every use."""

    def __init__(self, db: Database, secret: bytes, max_age_seconds: int) -> None:
        self._db = db
        self._serializer = URLSafeTimedSerializer(secret, salt="ow-session")
        self.max_age = max_age_seconds

    def issue(self, user_id: int, session_ver: int) -> str:
        return self._serializer.dumps({"u": user_id, "v": session_ver})

    def resolve(self, token: str | None) -> SessionUser | None:
        if not token:
            return None
        try:
            data = self._serializer.loads(token, max_age=self.max_age)
            user_id, ver = int(data["u"]), int(data["v"])
        except BadSignature, KeyError, TypeError, ValueError:
            return None
        row = self._db.one(
            "SELECT id, name, display_name, is_admin, disabled, session_ver "
            "FROM users WHERE id = ?",
            [user_id],
        )
        if row is None or row["disabled"] or row["session_ver"] != ver:
            return None
        return SessionUser(row["id"], row["name"], row["display_name"], row["is_admin"])


class LoginRateLimiter:
    """Refuses logins after too many recent failures, per client address and per user name."""

    def __init__(self, max_failures: int, window_seconds: int) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def blocked(self, *keys: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return any(len(self._recent(k, now)) >= self.max_failures for k in keys)

    def failure(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for k in keys:
                self._recent(k, now).append(now)

    def success(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._failures.pop(k, None)


def authenticate(db: Database, name: str, password: str) -> dict | None:
    """Return the user row if the name and password match and the account is enabled."""
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
