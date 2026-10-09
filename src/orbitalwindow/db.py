# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""DuckDB access. The service process is the only writer; every call is serialised by one lock.

Schema changes are appended to ``MIGRATIONS`` and never edited once released.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb

MIGRATIONS: list[str] = [
    # 1: phase 1 core
    """
    CREATE SEQUENCE seq_users START 1;
    CREATE TABLE users (
        id INTEGER PRIMARY KEY DEFAULT nextval('seq_users'),
        name VARCHAR NOT NULL UNIQUE,
        display_name VARCHAR NOT NULL,
        pw_hash VARCHAR NOT NULL,
        is_admin BOOLEAN NOT NULL DEFAULT false,
        disabled BOOLEAN NOT NULL DEFAULT false,
        session_ver INTEGER NOT NULL DEFAULT 1,
        uid_gid INTEGER NOT NULL UNIQUE,
        mac VARCHAR NOT NULL UNIQUE,
        image_id INTEGER,
        cpus DOUBLE,          -- NULL = unlimited
        mem_gb DOUBLE,        -- NULL = unlimited
        quota_gb DOUBLE,      -- NULL = no quota
        stopped_by_admin BOOLEAN NOT NULL DEFAULT false,
        created_at TIMESTAMP NOT NULL DEFAULT current_timestamp
    );
    CREATE SEQUENCE seq_images START 1;
    CREATE TABLE images (
        id INTEGER PRIMARY KEY DEFAULT nextval('seq_images'),
        label VARCHAR NOT NULL,
        base_tag VARCHAR NOT NULL UNIQUE,
        image_ref VARCHAR NOT NULL,
        status VARCHAR NOT NULL DEFAULT 'building',  -- building | ready | failed
        built_at TIMESTAMP,
        enabled BOOLEAN NOT NULL DEFAULT true
    );
    CREATE SEQUENCE seq_builds START 1;
    CREATE TABLE builds (
        id INTEGER PRIMARY KEY DEFAULT nextval('seq_builds'),
        kind VARCHAR NOT NULL,       -- base | user
        target VARCHAR NOT NULL,
        status VARCHAR NOT NULL,     -- queued | running | ok | failed
        log VARCHAR NOT NULL DEFAULT '',
        started_at TIMESTAMP,
        finished_at TIMESTAMP
    );
    CREATE TABLE audit (
        ts TIMESTAMP NOT NULL DEFAULT current_timestamp,
        actor VARCHAR NOT NULL,
        action VARCHAR NOT NULL,
        target VARCHAR NOT NULL DEFAULT '',
        detail VARCHAR NOT NULL DEFAULT ''
    );
    CREATE TABLE settings (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL);
    -- Monotonic allocators (UID/MAC, GID). Never decremented, so numbers are never reused.
    CREATE TABLE counters (name VARCHAR PRIMARY KEY, value INTEGER NOT NULL);
    """,
    # 2: phase 2 images, add-ons and packages
    """
    CREATE TABLE pkg_index (image_id INTEGER NOT NULL, name VARCHAR NOT NULL);
    CREATE INDEX pkg_index_lookup ON pkg_index (image_id, name);
    CREATE TABLE user_packages (
        user_id INTEGER NOT NULL, name VARCHAR NOT NULL, PRIMARY KEY (user_id, name)
    );
    CREATE TABLE user_image (
        user_id INTEGER PRIMARY KEY,
        image_ref VARCHAR NOT NULL,
        built_from_image_id INTEGER,
        pkg_hash VARCHAR,         -- hash of everything that went into the last good build
        status VARCHAR NOT NULL,  -- queued | building | ready | failed
        built_at TIMESTAMP,
        build_id INTEGER          -- latest build (for its log)
    );
    CREATE TABLE addon_settings (
        key VARCHAR PRIMARY KEY,
        enabled BOOLEAN NOT NULL DEFAULT true,
        restricted_override BOOLEAN   -- NULL = use addon.yaml
    );
    CREATE TABLE addon_grants (user_id INTEGER NOT NULL, key VARCHAR NOT NULL,
                               PRIMARY KEY (user_id, key));
    CREATE TABLE user_addons (user_id INTEGER NOT NULL, key VARCHAR NOT NULL,
                              PRIMARY KEY (user_id, key));
    ALTER TABLE images ADD COLUMN docker_id VARCHAR;  -- image ID of the last good base build
    """,
    # 3: phase 4 groups and disk usage
    """
    CREATE SEQUENCE seq_groups START 1;
    CREATE TABLE groups (
        id INTEGER PRIMARY KEY DEFAULT nextval('seq_groups'),
        name VARCHAR NOT NULL UNIQUE,
        gid INTEGER NOT NULL UNIQUE,
        description VARCHAR NOT NULL DEFAULT '',
        quota_gb DOUBLE,      -- NULL = no quota
        created_at TIMESTAMP NOT NULL DEFAULT current_timestamp
    );
    CREATE TABLE group_members (group_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                                PRIMARY KEY (group_id, user_id));
    CREATE TABLE disk_usage (
        subject VARCHAR NOT NULL,   -- user | group | scratch
        name VARCHAR NOT NULL,
        bytes BIGINT NOT NULL,
        measured_at TIMESTAMP NOT NULL,
        PRIMARY KEY (subject, name)
    );
    """,
    # 4: phase 5 SSH keys
    """
    CREATE SEQUENCE seq_ssh_keys START 1;
    CREATE TABLE ssh_keys (
        id INTEGER PRIMARY KEY DEFAULT nextval('seq_ssh_keys'),
        user_id INTEGER NOT NULL,
        label VARCHAR NOT NULL,
        pubkey VARCHAR NOT NULL,
        fingerprint VARCHAR NOT NULL,
        added_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
        UNIQUE (user_id, fingerprint)
    );
    """,
]


class Database:
    def __init__(self, path: Path | str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(path))
        # Timestamps are stored as naive UTC (TIMESTAMP): TIMESTAMPTZ results would need pytz.
        self._con.execute("SET TimeZone = 'UTC'")
        self._lock = threading.RLock()
        self._depth = 0
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._con.close()

    def _migrate(self) -> None:
        with self._lock:
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = self._con.execute("SELECT max(version) FROM schema_version").fetchone()
            current = row[0] or 0
            for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
                with self.transaction():
                    self._con.execute(sql)
                    self._con.execute("INSERT INTO schema_version VALUES (?)", [version])

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a block atomically. Nested use joins the outer transaction."""
        with self._lock:
            # Only the thread holding the lock touches _depth, so a plain attribute is safe.
            self._depth += 1
            try:
                if self._depth > 1:
                    yield
                    return
                self._con.execute("BEGIN TRANSACTION")
                try:
                    yield
                except BaseException:
                    self._con.execute("ROLLBACK")
                    raise
                self._con.execute("COMMIT")
            finally:
                self._depth -= 1

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        with self._lock:
            self._con.execute(sql, params or [])

    def all(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._con.execute(sql, params or [])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def one(self, sql: str, params: list[Any] | None = None) -> dict[str, Any] | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: list[Any] | None = None) -> Any:
        with self._lock:
            row = self._con.execute(sql, params or []).fetchone()
            return row[0] if row else None

    # --- small helpers used across modules -------------------------------------------------

    def next_counter(self, name: str) -> int:
        """Return the next value (1, 2, …) of a never-reused counter."""
        with self.transaction():
            value = self.scalar("SELECT value FROM counters WHERE name = ?", [name])
            if value is None:
                value = 1
                self.execute("INSERT INTO counters VALUES (?, ?)", [name, value])
            else:
                value += 1
                self.execute("UPDATE counters SET value = ? WHERE name = ?", [value, name])
            return value

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        value = self.scalar("SELECT value FROM settings WHERE key = ?", [key])
        return default if value is None else value

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [key, value],
        )

    def audit(self, actor: str, action: str, target: str = "", detail: str = "") -> None:
        self.execute(
            "INSERT INTO audit (actor, action, target, detail) VALUES (?, ?, ?, ?)",
            [actor, action, target, detail],
        )
