# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""System backups (#82, ADR 0046): the databases and what can't be made again, every day.

A backup is one file, ``$OW_ROOT/backups/ow-backup-<UTC time>.tar``. A one-off container from the
database server's own image (so ``pg_dump`` matches the server) makes it on the internal database
network. It holds:

- ``<database>.dump``: every database on the server (``pg_dump -Fc``), and ``databases`` with
  their owners;
- ``ca.tar.gz``: the private CA (ADR 0044), so users' PCs keep trusting the server;
- ``samba.tar.gz``: Samba's password database; ``sshpiper.tar.gz``: the SSH gateway's keys;
- ``state.tar.gz``: ``state/`` without the secrets that are made again by themselves (session key,
  API keys, database role passwords);
- ``SHA256SUMS``.

User data (homes, group folders) is not in it; ``make backup`` copies that. The admin copies the
backup files off the server (download on the Backups page, or ``scp``).
"""

import contextlib
import datetime as dt
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import docker

from . import pg
from .config import Settings
from .db import Database
from .users import ValidationError

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^ow-backup-\d{8}-\d{6}\.tar$")
LABEL_ROLE = "ow.role"
ROLE = "backup"
DEFAULT_TIME = "03:00"
DEFAULT_KEEP = 5
POLL_SECONDS = 30
# state/ entries that are made again when missing, so a backup leaves them out.
STATE_EXCLUDE = ("secret.key", "ai_api_key", "openwebui_secret", "db_*_password", "*.duckdb*")

SCRIPT = r"""
set -eu
umask 077
work="/backups/.$NAME.work"
rm -rf "$work"
mkdir "$work"
trap 'rm -rf "$work" "/backups/.$NAME.part"' EXIT
psql -d postgres -Atc "SELECT datname || ' ' || pg_get_userbyid(datdba) FROM pg_database
  WHERE datallowconn AND NOT datistemplate AND datname <> 'postgres' ORDER BY 1" > "$work/databases"
while read -r database owner; do
  pg_dump -Fc -d "$database" -f "$work/$database.dump"
done < "$work/databases"
tar -C /ca -czf "$work/ca.tar.gz" .
tar -C /samba -czf "$work/samba.tar.gz" --exclude=./private/msg.sock .
tar -C /sshpiper -czf "$work/sshpiper.tar.gz" .
tar -C /state -czf "$work/state.tar.gz" $STATE_EXCLUDE .
(cd "$work" && sha256sum -- * > SHA256SUMS)
tar -C "$work" -cf "/backups/.$NAME.part" .
chown "$OWNER" "/backups/.$NAME.part"
chmod 600 "/backups/.$NAME.part"
mv "/backups/.$NAME.part" "/backups/$NAME"
"""


def parse_time(value: str) -> dt.time:
    """Read a time of day as ``HH:MM`` (24 hours).

    Args:
        value: The time as entered.

    Returns:
        The time.

    Raises:
        ValidationError: When it is not a valid ``HH:MM``.
    """
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValidationError("Enter the time as HH:MM, for example 03:00.")
    return dt.time(int(match[1]), int(match[2]))


def due(now: dt.datetime, at: dt.time, last_day: str) -> bool:
    """Tell whether the daily backup should run now.

    A server that was off at the time makes the backup when it is back the same day.

    Args:
        now: The local time now.
        at: Local time of day of the daily backup.
        last_day: Local date (ISO) of the last scheduled backup; empty when there was none.

    Returns:
        True when today's backup time has passed and today had no scheduled backup yet.
    """
    return now.time() >= at and last_day != now.date().isoformat()


class Backups:
    def __init__(self, client: docker.DockerClient, settings: Settings, db: Database) -> None:
        """Create the backup manager.

        Args:
            client: Docker client (through the socket proxy).
            settings: Application settings (folders, volumes, database server).
            db: The database (backup settings and the result of the last backup).
        """
        self._client = client
        self._settings = settings
        self._db = db
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.running = False

    @property
    def folder(self) -> Path:
        """Return the folder that holds the backups (``$OW_ROOT/backups``).

        Returns:
            The folder.
        """
        return self._settings.root / "backups"

    # --- settings ----------------------------------------------------------------------------

    def config(self) -> dict[str, Any]:
        """Return the backup settings and the result of the last backup.

        Returns:
            ``enabled``, ``time`` (``HH:MM``), ``keep``, and ``last_at``, ``last_status``
            (``ok`` or ``failed``), ``last_error``, ``last_file`` (empty before the first).
        """
        get = self._db.get_setting
        return {
            "enabled": get("backup.enabled", "true") == "true",
            "time": get("backup.time", DEFAULT_TIME),
            "keep": int(get("backup.keep", str(DEFAULT_KEEP))),
            "last_at": get("backup.last_at", ""),
            "last_status": get("backup.last_status", ""),
            "last_error": get("backup.last_error", ""),
            "last_file": get("backup.last_file", ""),
        }

    def configure(self, actor: str, enabled: bool, time: str, keep: int) -> None:
        """Change the backup settings.

        Args:
            actor: Admin making the change.
            enabled: Whether the daily backup runs.
            time: Local time of the daily backup, ``HH:MM``.
            keep: How many backups to keep (1-100); older ones are deleted after a backup.

        Raises:
            ValidationError: When the time or the number is not valid.
        """
        at = parse_time(time)
        if not 1 <= keep <= 100:
            raise ValidationError("Keep between 1 and 100 backups.")
        old = self.config()
        new = {"enabled": enabled, "time": at.strftime("%H:%M"), "keep": keep}
        with self._db.transaction():
            self._db.set_setting("backup.enabled", "true" if enabled else "false")
            self._db.set_setting("backup.time", new["time"])
            self._db.set_setting("backup.keep", str(keep))
            changes = [f"{key}={value}" for key, value in new.items() if old[key] != value]
            if changes:
                action = "backup.off" if old["enabled"] and not enabled else "backup.settings"
                self._db.audit(actor, action, "", ", ".join(changes))
        self._wake.set()

    def warning(self) -> str:
        """Return what an admin must know about the backups, if anything.

        Returns:
            A warning when the daily backup is off or the last one failed; else empty.
        """
        config = self.config()
        if not config["enabled"]:
            return "Daily backups are off. If the server's disk fails, everything is lost."
        if config["last_status"] == "failed":
            return f"The last backup failed: {config['last_error']}"
        return ""

    # --- backups -----------------------------------------------------------------------------

    def entries(self) -> list[dict[str, Any]]:
        """Return the backups in the folder, newest first.

        Returns:
            ``name``, ``size`` (bytes) and ``time`` (UTC, from the name) of each backup.
        """
        try:
            names = [p.name for p in self.folder.iterdir() if NAME_RE.match(p.name)]
        except FileNotFoundError:
            return []
        rows = []
        for name in sorted(names, reverse=True):
            stamp = dt.datetime.strptime(name[10:25], "%Y%m%d-%H%M%S").replace(tzinfo=dt.UTC)
            rows.append({"name": name, "size": (self.folder / name).stat().st_size, "time": stamp})
        return rows

    def path(self, name: str) -> Path:
        """Return the path of one backup, for a download.

        Args:
            name: The backup's file name.

        Returns:
            The path.

        Raises:
            FileNotFoundError: When there is no backup with that name.
        """
        if not NAME_RE.match(name) or not (self.folder / name).is_file():
            raise FileNotFoundError(name)
        return self.folder / name

    def run(self, actor: str) -> str:
        """Make a backup now, then delete the oldest ones beyond the number to keep.

        Args:
            actor: Who asked for it (``system`` for the daily backup).

        Returns:
            The new backup's file name.

        Raises:
            RuntimeError: When a backup is already running, or the backup failed.
        """
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("A backup is already running.")
        self.running = True
        try:
            name = dt.datetime.now(dt.UTC).strftime("ow-backup-%Y%m%d-%H%M%S.tar")
            try:
                self._make(name)
            except Exception as exc:
                error = str(exc).strip().splitlines()[-1][:300] if str(exc).strip() else repr(exc)
                log.error("backup failed: %s", error)
                self._record(actor, "failed", error, "")
                raise RuntimeError(error) from exc
            removed = self._prune(self.config()["keep"])
            self._record(actor, "ok", "", name, removed)
            return name
        finally:
            self.running = False
            self._lock.release()

    def _make(self, name: str) -> None:
        """Run the backup container and wait for it.

        Args:
            name: File name of the new backup.

        Raises:
            RuntimeError: When the container fails; its last output lines are the message.
        """
        settings = self._settings
        password = settings.db_password.get_secret_value() if settings.db_password else ""
        excludes = " ".join(f"--exclude='./{pattern}'" for pattern in STATE_EXCLUDE)
        container = self._client.containers.run(
            settings.db_image,
            ["sh", "-c", SCRIPT.replace("$STATE_EXCLUDE", excludes)],
            detach=True,
            network=pg.NETWORK,
            environment={
                "NAME": name,
                "OWNER": f"{os.getuid()}:{os.getgid()}",
                "PGHOST": settings.db_host,
                "PGPORT": str(settings.db_port),
                "PGUSER": settings.db_user,
                "PGPASSWORD": password,
            },
            volumes={
                str(self.folder): {"bind": "/backups", "mode": "rw"},
                str(settings.state_dir): {"bind": "/state", "mode": "ro"},
                settings.ca_volume: {"bind": "/ca", "mode": "ro"},
                settings.samba_volume: {"bind": "/samba", "mode": "ro"},
                settings.sshpiper_volume: {"bind": "/sshpiper", "mode": "ro"},
            },
            labels={"ow.managed": "true", LABEL_ROLE: ROLE},
            security_opt=["no-new-privileges"],
        )
        try:
            result = container.wait(timeout=3600)
            if result.get("StatusCode") != 0:
                output = container.logs(tail=5).decode(errors="replace").strip()
                raise RuntimeError(output or f"exit status {result.get('StatusCode')}")
        finally:
            container.remove(force=True)

    def _prune(self, keep: int) -> list[str]:
        """Delete the oldest backups beyond ``keep``.

        Args:
            keep: How many backups to keep.

        Returns:
            The names of the deleted backups.
        """
        removed = []
        for row in self.entries()[keep:]:
            (self.folder / row["name"]).unlink(missing_ok=True)
            removed.append(row["name"])
        return removed

    def _record(
        self, actor: str, status: str, error: str, name: str, removed: list[str] | None = None
    ) -> None:
        """Store the result of a backup and audit it.

        Args:
            actor: Who asked for the backup.
            status: ``ok`` or ``failed``.
            error: The error message of a failed backup.
            name: File name of a good backup.
            removed: Old backups deleted after it.
        """
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None).isoformat(timespec="seconds")
        with self._db.transaction():
            self._db.set_setting("backup.last_at", now)
            self._db.set_setting("backup.last_status", status)
            self._db.set_setting("backup.last_error", error)
            if name:
                self._db.set_setting("backup.last_file", name)
            detail = name or error
            if removed:
                detail += f" (removed {', '.join(removed)})"
            self._db.audit(actor, f"backup.{status}", "", detail)

    # --- daily backup ------------------------------------------------------------------------

    def start(self) -> None:
        """Remove a backup container left by a restart, then start the daily backup thread."""
        try:
            for container in self._client.containers.list(
                all=True, filters={"label": f"{LABEL_ROLE}={ROLE}"}
            ):
                log.warning("removing %s, left by a restart during a backup", container.name)
                container.remove(force=True)
        except docker.errors.DockerException as exc:
            log.warning("could not look for old backup containers: %s", exc)
        threading.Thread(target=self._loop, name="ow-backups", daemon=True).start()

    def stop(self) -> None:
        """Stop the daily backup thread."""
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        """Check every ``POLL_SECONDS`` whether the daily backup is due (the backup thread)."""
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("daily backup failed")
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    def step(self, now: dt.datetime | None = None) -> bool:
        """Make the daily backup when it is due.

        Args:
            now: The local time; the server's clock in ``TZ`` when None.

        Returns:
            True when a backup was started (whether or not it worked).
        """
        config = self.config()
        if not config["enabled"]:
            return False
        now = now or dt.datetime.now(ZoneInfo(self._settings.tz))
        last_day = self._db.get_setting("backup.last_scheduled", "") or ""
        if not due(now, parse_time(config["time"]), last_day):
            return False
        # Recorded first: a failing backup is tried again tomorrow, not every 30 seconds.
        self._db.set_setting("backup.last_scheduled", now.date().isoformat())
        with contextlib.suppress(RuntimeError):  # recorded and shown on the admin pages
            self.run("system")
        return True
