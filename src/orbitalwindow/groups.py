# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Group/project folders: names, GID allocation and membership.

Group names share one namespace with user names: both become Samba shares (``\\\\server\\<name>``)
and Unix names in the desktops and the Samba container.
"""

from typing import Any

from .db import Database
from .users import NAME_RE, RESERVED_NAMES, ValidationError

GID_BASE = 3000
MAX_GROUPS = 999  # GIDs 3001–3999; user UIDs start at 2001 and never reach 3000 in practice


def validate_group_name(db: Database, name: str) -> str:
    """Normalise and check a new group name.

    Args:
        db: The application database.
        name: The requested name (case and surrounding spaces are ignored).

    Returns:
        The lowercased, stripped name.

    Raises:
        ValidationError: If the name has the wrong form, is reserved or is already used by a
            user or group.
    """
    name = name.strip().lower()
    if not NAME_RE.fullmatch(name):
        raise ValidationError(
            "A group name is 2–31 characters: lowercase letters and digits, starting with a letter."
        )
    if name in RESERVED_NAMES:
        raise ValidationError(f"'{name}' is reserved. Please choose another name.")
    if db.scalar("SELECT count(*) FROM users WHERE name = ?", [name]):
        raise ValidationError(f"There is already a user called '{name}'.")
    if db.scalar("SELECT count(*) FROM groups WHERE name = ?", [name]):
        raise ValidationError(f"There is already a group called '{name}'.")
    return name


def insert_group(
    db: Database, name: str, description: str, quota_gb: float | None
) -> dict[str, Any]:
    """Validate and store a new group with the next free GID.

    Args:
        db: The application database.
        name: The new group name.
        description: Free text (trimmed to 200 characters).
        quota_gb: Folder quota in GB, or None for unlimited.

    Returns:
        The stored group row.

    Raises:
        ValidationError: If the name is not valid or no more group numbers are available.
    """
    with db.transaction():
        name = validate_group_name(db, name)
        number = db.next_counter("group")
        if number > MAX_GROUPS:
            raise ValidationError("No more group numbers are available.")
        db.execute(
            "INSERT INTO groups (name, gid, description, quota_gb) VALUES (?, ?, ?, ?)",
            [name, GID_BASE + number, description.strip()[:200], quota_gb],
        )
        return db.one("SELECT * FROM groups WHERE name = ?", [name])  # type: ignore[return-value]


def get_group(db: Database, group_id: int) -> dict[str, Any] | None:
    """Return a group row by id.

    Args:
        db: The application database.
        group_id: The group's database id.

    Returns:
        The group row, or None if there is no such group.
    """
    return db.one("SELECT * FROM groups WHERE id = ?", [group_id])


def list_groups(db: Database) -> list[dict[str, Any]]:
    """Return every group with its member count.

    Args:
        db: The application database.

    Returns:
        Group rows with a ``members`` count, sorted by name.
    """
    return db.all(
        "SELECT g.*, (SELECT count(*) FROM group_members m WHERE m.group_id = g.id) AS members "
        "FROM groups g ORDER BY g.name"
    )


def members(db: Database, group_id: int) -> list[dict[str, Any]]:
    """Return the members of a group.

    Args:
        db: The application database.
        group_id: The group's database id.

    Returns:
        Member ids, names and display names, sorted by name.
    """
    return db.all(
        "SELECT u.id, u.name, u.display_name FROM group_members m "
        "JOIN users u ON u.id = m.user_id WHERE m.group_id = ? ORDER BY u.name",
        [group_id],
    )


def groups_of(db: Database, user_id: int) -> list[dict[str, Any]]:
    """Return the groups a user belongs to.

    Args:
        db: The application database.
        user_id: The user's database id.

    Returns:
        Group rows, sorted by name.
    """
    return db.all(
        "SELECT g.* FROM group_members m JOIN groups g ON g.id = m.group_id "
        "WHERE m.user_id = ? ORDER BY g.name",
        [user_id],
    )
