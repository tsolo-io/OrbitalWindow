# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""User accounts: names, UID/MAC allocation, creation and passwords."""

import re
from dataclasses import dataclass
from typing import Any

from .auth import hash_password
from .db import Database

UID_BASE = 2000
MAX_USERS = 0xFFFF

# Lowercase letters and digits only. A hyphen would let ``files-bob`` collide with the
# file manager container of ``bob`` (``ow-files-bob``); an underscore is not valid in hostnames.
NAME_RE = re.compile(r"^[a-z][a-z0-9]{1,30}$")
RESERVED_NAMES = frozenset(
    {
        "abc", "adm", "admin", "administrator", "bin", "daemon", "desktop", "files", "global",
        "groups", "guest", "homes", "ipc", "lp", "mail", "netlogon", "nobody", "nogroup",
        "operator", "orbitalwindow", "printers", "profiles", "proxy", "root", "samba",
        "scratch", "shared", "smb", "sshd", "staff", "sudo", "sync", "sys", "system",
        "traefik", "users", "www", "wwwdata",
    }
)  # fmt: skip


class ValidationError(ValueError):
    """A user-facing validation message."""


@dataclass(frozen=True)
class Allocation:
    number: int
    uid_gid: int
    mac: str


def validate_name(name: str, *, allow_reserved: bool = False) -> str:
    name = name.strip().lower()
    if not NAME_RE.fullmatch(name):
        raise ValidationError(
            "A user name is 2–31 characters: lowercase letters and digits, starting with a letter."
        )
    if name in RESERVED_NAMES and not allow_reserved:
        raise ValidationError(f"'{name}' is reserved. Please choose another name.")
    return name


def validate_password(password: str, min_length: int) -> None:
    if len(password) < min_length:
        raise ValidationError(f"The password must be at least {min_length} characters long.")
    if len(password) > 256:
        raise ValidationError("The password is too long (256 characters at most).")
    if password != password.strip():
        raise ValidationError("The password must not start or end with a space.")


def mac_for(number: int) -> str:
    return f"02:42:de:5c:{(number >> 8) & 0xFF:02x}:{number & 0xFF:02x}"


def allocate(db: Database) -> Allocation:
    """Next UID and MAC. Backed by a counter, so numbers of deleted users are never reused."""
    number = db.next_counter("user")
    if number > MAX_USERS:
        raise ValidationError("No more user numbers are available.")
    return Allocation(number, UID_BASE + number, mac_for(number))


def get_user(db: Database, user_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM users WHERE id = ?", [user_id])


def get_user_by_name(db: Database, name: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM users WHERE name = ?", [name])


def insert_user(
    db: Database,
    *,
    name: str,
    display_name: str,
    password: str,
    is_admin: bool,
    image_id: int | None,
    cpus: float | None,
    mem_gb: float | None,
    quota_gb: float | None,
    allow_reserved: bool = False,
) -> dict[str, Any]:
    """Validate and store a new user row. Side effects (home, Samba) are the caller's job."""
    name = validate_name(name, allow_reserved=allow_reserved)
    with db.transaction():
        if get_user_by_name(db, name):
            raise ValidationError(f"A user called '{name}' already exists.")
        if db.scalar("SELECT count(*) FROM groups WHERE name = ?", [name]):
            raise ValidationError(f"There is already a group called '{name}'.")
        alloc = allocate(db)
        db.execute(
            """INSERT INTO users (name, display_name, pw_hash, is_admin, uid_gid, mac, image_id,
                                  cpus, mem_gb, quota_gb)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                name,
                display_name.strip() or name,
                hash_password(password),
                is_admin,
                alloc.uid_gid,
                alloc.mac,
                image_id,
                cpus,
                mem_gb,
                quota_gb,
            ],
        )
        return get_user_by_name(db, name)  # type: ignore[return-value]


def set_password_hash(db: Database, user_id: int, password: str) -> int:
    """Store a new hash and bump the session version (signs out other sessions). Returns it."""
    with db.transaction():
        db.execute(
            "UPDATE users SET pw_hash = ?, session_ver = session_ver + 1 WHERE id = ?",
            [hash_password(password), user_id],
        )
        return db.scalar("SELECT session_ver FROM users WHERE id = ?", [user_id])


def parse_limit(value: str | float | None) -> float | None:
    """'' / None / 0 → unlimited (None); otherwise a positive number."""
    if value is None or value == "":
        return None
    number = float(value)
    if number < 0:
        raise ValidationError("Limits cannot be negative.")
    return number or None
