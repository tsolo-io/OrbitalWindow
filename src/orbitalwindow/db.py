# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""DuckDB access. The service process is the only writer; every call is serialised by one lock.

Schema changes are appended to ``MIGRATIONS`` and never edited once released.
"""

import logging
import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb

log = logging.getLogger(__name__)


def _exit_process() -> None:
    """Flush logs and stderr, then exit the process immediately with status 70."""
    logging.shutdown()
    sys.stderr.flush()
    os._exit(70)


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
    # 5: drop the secondary index on pkg_index. It went out of step with the table on the test
    # server, and DuckDB then invalidated the whole database on the next DELETE. A scan of
    # ~80k rows per desktop is fast enough, so the index is not worth the risk.
    """
    DROP INDEX IF EXISTS pkg_index_lookup;
    """,
    # 6: AI manager role and model downloads
    """
    ALTER TABLE users ADD COLUMN is_ai_manager BOOLEAN DEFAULT false;
    UPDATE users SET is_ai_manager = false;
    CREATE SEQUENCE seq_model_downloads START 1;
    CREATE TABLE model_downloads (
        id INTEGER PRIMARY KEY DEFAULT nextval('seq_model_downloads'),
        repo VARCHAR NOT NULL,          -- Hugging Face repo id, e.g. Qwen/Qwen3-8B
        revision VARCHAR NOT NULL,      -- commit the download is pinned to
        status VARCHAR NOT NULL,        -- queued | running | cancelling | done | failed | cancelled
        bytes_total BIGINT NOT NULL,
        bytes_done BIGINT NOT NULL DEFAULT 0,
        error VARCHAR NOT NULL DEFAULT '',
        requested_by VARCHAR NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
        started_at TIMESTAMP,
        finished_at TIMESTAMP
    );
    """,
    # 7: AI sessions (one vLLM container at a time; a row per start)
    """
    CREATE SEQUENCE seq_ai_sessions START 1;
    CREATE TABLE ai_sessions (
        id INTEGER PRIMARY KEY DEFAULT nextval('seq_ai_sessions'),
        repo VARCHAR NOT NULL,
        gpu_index INTEGER NOT NULL,
        context INTEGER NOT NULL,
        gpu_share DOUBLE NOT NULL,
        public BOOLEAN NOT NULL,       -- /v1 reachable through Traefik
        status VARCHAR NOT NULL,       -- started | stopped | failed
        error VARCHAR NOT NULL DEFAULT '',
        started_by VARCHAR NOT NULL,
        started_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
        stopped_at TIMESTAMP
    );
    """,
    # 8: coding benchmarks of models (EvalPlus)
    """
    CREATE SEQUENCE seq_benchmarks START 1;
    CREATE TABLE benchmarks (
        id INTEGER PRIMARY KEY DEFAULT nextval('seq_benchmarks'),
        repo VARCHAR NOT NULL,
        suite VARCHAR NOT NULL,         -- humaneval | mbpp
        status VARCHAR NOT NULL,        -- queued | starting | generating | evaluating | cancelling
                                        -- | done | failed | cancelled
        done INTEGER NOT NULL DEFAULT 0,
        total INTEGER NOT NULL,
        tasks INTEGER,
        base_pass INTEGER,
        plus_pass INTEGER,
        base_score DOUBLE,
        plus_score DOUBLE,
        gpu_index INTEGER,              -- session settings the model ran with
        context INTEGER,
        gpu_share DOUBLE,
        error VARCHAR NOT NULL DEFAULT '',
        started_by VARCHAR NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
        started_at TIMESTAMP,
        finished_at TIMESTAMP
    );
    """,
    # 9: users' own environment variables (#41)
    """
    CREATE TABLE user_env (
        user_id INTEGER NOT NULL,
        name VARCHAR NOT NULL,
        value VARCHAR NOT NULL,
        updated_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
        PRIMARY KEY (user_id, name)
    );
    """,
]


class Database:
    def __init__(self, path: Path | str) -> None:
        """Open (or create) the database and apply pending migrations.

        Args:
            path: Database file path, or ``":memory:"`` for an in-memory database.
        """
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(path))
        # Timestamps are stored as naive UTC (TIMESTAMP): TIMESTAMPTZ results would need pytz.
        self._con.execute("SET TimeZone = 'UTC'")
        self._lock = threading.RLock()
        self._depth = 0
        # Called on a DuckDB fatal error; tests replace it.
        self.on_fatal: Callable[[], None] = _exit_process
        self._migrate()

    def close(self) -> None:
        """Close the DuckDB connection."""
        with self._lock:
            self._con.close()

    def _migrate(self) -> None:
        """Apply every migration in ``MIGRATIONS`` newer than the recorded schema version."""
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
        """Run a block atomically. Nested use joins the outer transaction.

        Yields:
            Nothing; the block runs inside the transaction and is rolled back if it raises.
        """
        with self._lock:
            # Only the thread holding the lock touches _depth, so a plain attribute is safe.
            self._depth += 1
            try:
                if self._depth > 1:
                    yield
                    return
                self._run("BEGIN TRANSACTION")
                try:
                    yield
                except BaseException:
                    self._run("ROLLBACK")
                    raise
                self._run("COMMIT")
            finally:
                self._depth -= 1

    def _run(self, sql: str, params: list[Any] | None = None) -> duckdb.DuckDBPyConnection:
        """Execute one statement.

        A DuckDB *fatal* error invalidates the database for the rest of the process, so the
        process exits and Docker restarts it (DuckDB recovers from its WAL) instead of failing
        every request until someone notices.

        Args:
            sql: SQL statement.
            params: Positional query parameters.

        Returns:
            The DuckDB connection, ready to fetch results.
        """
        try:
            return self._con.execute(sql, params or [])
        except duckdb.FatalException:
            log.critical("DuckDB fatal error; exiting so the service restarts", exc_info=True)
            self.on_fatal()
            raise

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        """Execute one statement under the lock, discarding any result.

        Args:
            sql: SQL statement.
            params: Positional query parameters.
        """
        with self._lock:
            self._run(sql, params)

    def all(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Run a query and return every row.

        Args:
            sql: SQL query.
            params: Positional query parameters.

        Returns:
            One dict per row, keyed by column name.
        """
        with self._lock:
            cur = self._run(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def one(self, sql: str, params: list[Any] | None = None) -> dict[str, Any] | None:
        """Run a query and return its first row.

        Args:
            sql: SQL query.
            params: Positional query parameters.

        Returns:
            The first row as a dict keyed by column name, or None if there are no rows.
        """
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: list[Any] | None = None) -> Any:
        """Run a query and return the first column of its first row.

        Args:
            sql: SQL query.
            params: Positional query parameters.

        Returns:
            The value, or None if there are no rows.
        """
        with self._lock:
            row = self._run(sql, params).fetchone()
            return row[0] if row else None

    # --- small helpers used across modules -------------------------------------------------

    def next_counter(self, name: str) -> int:
        """Return the next value (1, 2, …) of a never-reused counter.

        Args:
            name: Counter name.

        Returns:
            The new counter value.
        """
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
        """Return a stored setting.

        Args:
            key: Setting name.
            default: Value returned when the setting is not stored.

        Returns:
            The stored value, or ``default``.
        """
        value = self.scalar("SELECT value FROM settings WHERE key = ?", [key])
        return default if value is None else value

    def set_setting(self, key: str, value: str) -> None:
        """Store a setting, replacing any existing value.

        Args:
            key: Setting name.
            value: New value.
        """
        self.execute(
            "INSERT INTO settings VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [key, value],
        )

    def audit(self, actor: str, action: str, target: str = "", detail: str = "") -> None:
        """Append an entry to the audit log.

        Args:
            actor: Who performed the action.
            action: What was done.
            target: What it was done to.
            detail: Free-form extra detail.
        """
        self.execute(
            "INSERT INTO audit (actor, action, target, detail) VALUES (?, ?, ?, ?)",
            [actor, action, target, detail],
        )
