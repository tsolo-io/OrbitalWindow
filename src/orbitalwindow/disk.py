# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Disk usage (measured hourly), soft quotas and the nightly scratch clean-up.

Quotas are soft: users see a warning at 90%, admins see everyone. Nothing is enforced.
"""

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import Database
from .hostops import HostOps

log = logging.getLogger(__name__)

WARN_FRACTION = 0.9  # a user or group at ≥90% of its quota sees a warning
HOST_WARN_FRACTION = 0.85  # the admin page warns when the filesystem is ≥85% full


@dataclass(frozen=True)
class Usage:
    subject: str  # user | group | scratch
    name: str
    bytes: int
    quota_gb: float | None
    measured_at: datetime | None

    @property
    def fraction(self) -> float | None:
        """Return how much of the quota is used.

        Returns:
            Used bytes divided by the quota, or None when there is no quota.
        """
        if not self.quota_gb:
            return None
        return self.bytes / (self.quota_gb * 1024**3)

    @property
    def warning(self) -> bool:
        """Tell whether the usage is at or above ``WARN_FRACTION`` of the quota.

        Returns:
            True if a warning should be shown.
        """
        return self.fraction is not None and self.fraction >= WARN_FRACTION


@dataclass(frozen=True)
class HostSpace:
    total: int
    free: int

    @property
    def used_fraction(self) -> float:
        """Return how full the filesystem is.

        Returns:
            The used fraction (0.0 when the size is unknown).
        """
        return 1 - self.free / self.total if self.total else 0.0

    @property
    def warning(self) -> bool:
        """Tell whether the filesystem is at or above ``HOST_WARN_FRACTION`` full.

        Returns:
            True if a warning should be shown.
        """
        return self.used_fraction >= HOST_WARN_FRACTION


def host_space(root: Path) -> HostSpace:
    """Return the size and free space of the filesystem holding ``root``.

    Args:
        root: A path on the filesystem.

    Returns:
        Total and available bytes.
    """
    st = os.statvfs(root)
    return HostSpace(st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize)


def _subject_of(path: str) -> tuple[str, str] | None:
    """Map a measured path to its subject: 'users/bob' → ('user', 'bob'); scratch.

    'groups/labx' → ('group', 'labx'); 'shared/scratch' → ('scratch', 'scratch').

    Args:
        path: A path relative to ``$OW_ROOT``.

    Returns:
        ``(subject, name)``, or None for a path that is not a home, group folder or scratch.
    """
    if path == "shared/scratch":
        return "scratch", "scratch"
    kind, _, name = path.partition("/")
    if kind == "users" and name:
        return "user", name
    if kind == "groups" and name:
        return "group", name
    return None


def measure(db: Database, hostops: HostOps) -> int:
    """Run ``du`` over homes, group folders and scratch, and store the results.

    Args:
        db: The application database.
        hostops: Helper that runs ``du`` with root access.

    Returns:
        The number of stored measurements.
    """
    sizes = hostops.disk_usage()
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = []
    for path, size in sizes.items():
        subject = _subject_of(path)
        if subject:
            rows.append((*subject, size))
    with db.transaction():
        db.execute("DELETE FROM disk_usage")
        for subject, name, size in rows:
            db.execute("INSERT INTO disk_usage VALUES (?, ?, ?, ?)", [subject, name, size, now])
    return len(rows)


def usage_for_user(db: Database, user: dict[str, Any]) -> Usage:
    """Return the last measured usage of a user's home.

    Args:
        db: The application database.
        user: The user row.

    Returns:
        The usage, with 0 bytes and no time when it was not measured yet.
    """
    row = db.one("SELECT * FROM disk_usage WHERE subject = 'user' AND name = ?", [user["name"]])
    return Usage(
        "user",
        user["name"],
        row["bytes"] if row else 0,
        user.get("quota_gb"),
        row["measured_at"] if row else None,
    )


def all_usage(db: Database) -> list[Usage]:
    """Return the usage of everyone (users, groups, scratch), largest first.

    Args:
        db: The application database.

    Returns:
        The last measurements with their quotas.
    """
    rows = db.all(
        "SELECT d.*, coalesce(u.quota_gb, g.quota_gb) AS quota_gb FROM disk_usage d "
        "LEFT JOIN users u ON d.subject = 'user' AND u.name = d.name "
        "LEFT JOIN groups g ON d.subject = 'group' AND g.name = d.name "
        "ORDER BY d.bytes DESC"
    )
    return [
        Usage(row["subject"], row["name"], row["bytes"], row["quota_gb"], row["measured_at"])
        for row in rows
    ]


def clean_scratch(hostops: HostOps, days: int) -> int:
    """Delete scratch files untouched for ``days``.

    Args:
        hostops: Helper that deletes the files with root access.
        days: Age in days after which an unchanged file is deleted.

    Returns:
        The number of removed files and folders.
    """
    removed = hostops.clean_scratch(days)
    if removed:
        log.info("scratch clean-up removed %d item(s) older than %d days", len(removed), days)
    return len(removed)
