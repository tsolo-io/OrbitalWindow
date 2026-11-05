# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""System backups (#82)."""

import datetime as dt
from types import SimpleNamespace

import pytest
from conftest import login

from orbitalwindow.backups import Backups, due, parse_time
from orbitalwindow.users import ValidationError


class FakeContainer:
    def __init__(self, folder, environment, status):
        """Remember where the backup goes and how the run ends."""
        self.folder, self.environment, self.status = folder, environment, status
        self.removed = False

    def wait(self, timeout=None):
        """Write the backup file (like the script) when the run succeeds."""
        if self.status == 0:
            (self.folder / self.environment["NAME"]).write_bytes(b"tar")
        return {"StatusCode": self.status}

    def logs(self, tail=None):
        """Return the script's last output."""
        return b"pg_dump: error: connection to server failed"

    def remove(self, force=False):
        """Mark as removed."""
        self.removed = True


class FakeClient:
    def __init__(self, folder):
        """Record the runs; each one succeeds unless ``fail`` is set."""
        self.folder, self.runs, self.fail = folder, [], False
        self.containers = SimpleNamespace(run=self._run)

    def _run(self, image, command, **kwargs):
        """Record the run and return a fake container."""
        self.runs.append((image, command, kwargs))
        container = FakeContainer(self.folder, kwargs["environment"], 1 if self.fail else 0)
        self.last = container
        return container


@pytest.fixture
def backups(settings, database):
    """A backup manager with a fake Docker client and its folder in tmp_path."""
    folder = settings.root / "backups"
    folder.mkdir(parents=True)
    return Backups(FakeClient(folder), settings, database)


def test_time_of_day():
    """Times are HH:MM in 24 hours."""
    assert parse_time("3:00") == dt.time(3, 0)
    assert parse_time(" 23:59 ") == dt.time(23, 59)
    for bad in ("24:00", "3", "03:60", "three"):
        with pytest.raises(ValidationError):
            parse_time(bad)


def test_daily_backup_is_due_once_a_day_after_its_time():
    """Due after the time, once per day; a server that was off catches up the same day."""
    at = dt.time(3, 0)
    morning = dt.datetime(2026, 11, 5, 2, 59)
    assert not due(morning, at, "")
    assert due(morning.replace(hour=3), at, "")
    assert due(morning.replace(hour=9), at, "2026-11-04")
    assert not due(morning.replace(hour=9), at, "2026-11-05")


def test_defaults_and_settings(backups):
    """On by default at 03:00, keeping 5; changes are checked and audited."""
    config = backups.config()
    assert (config["enabled"], config["time"], config["keep"]) == (True, "03:00", 5)
    assert backups.warning() == ""
    backups.configure("boss", False, "4:30", 7)
    config = backups.config()
    assert (config["enabled"], config["time"], config["keep"]) == (False, "04:30", 7)
    assert "off" in backups.warning()
    audit = backups._db.one("SELECT action, detail FROM audit ORDER BY id DESC LIMIT 1")
    assert audit == {"action": "backup.off", "detail": "enabled=False, time=04:30, keep=7"}
    with pytest.raises(ValidationError):
        backups.configure("boss", True, "03:00", 0)
    with pytest.raises(ValidationError):
        backups.configure("boss", True, "25:00", 5)


def test_backup_runs_the_dump_container_and_keeps_the_newest(backups, settings):
    """A backup runs on the database network with read-only sources; old ones are deleted."""
    folder = backups.folder
    for day in range(1, 6):
        (folder / f"ow-backup-202611{day:02d}-030000.tar").write_bytes(b"old")
    (folder / "notes.txt").write_text("not a backup")
    name = backups.run("boss")
    image, command, kwargs = backups._client.runs[0]
    assert image == settings.db_image and kwargs["network"] == "ow-db"
    assert command[:2] == ["sh", "-c"] and "pg_dump -Fc" in command[2]
    assert "--exclude='./secret.key'" in command[2]
    modes = {bind["bind"]: bind["mode"] for bind in kwargs["volumes"].values()}
    assert modes == {"/backups": "rw", "/state": "ro", "/ca": "ro", "/samba": "ro",
                     "/sshpiper": "ro"}  # fmt: skip
    assert backups._client.last.removed
    names = [row["name"] for row in backups.entries()]
    assert names[0] == name and len(names) == 5
    assert "ow-backup-20261101-030000.tar" not in names and (folder / "notes.txt").exists()
    config = backups.config()
    assert (config["last_status"], config["last_file"]) == ("ok", name)
    detail = backups._db.scalar("SELECT detail FROM audit WHERE action = 'backup.ok'")
    assert detail == f"{name} (removed ow-backup-20261101-030000.tar)"


def test_failed_backup_is_recorded_and_shown(backups):
    """A failed backup keeps the old ones, and the admins see the error."""
    (backups.folder / "ow-backup-20261101-030000.tar").write_bytes(b"old")
    backups._client.fail = True
    with pytest.raises(RuntimeError, match="connection to server failed"):
        backups.run("system")
    assert len(backups.entries()) == 1
    assert backups.warning().startswith("The last backup failed: pg_dump: error")
    assert backups._db.scalar("SELECT count(*) FROM audit WHERE action = 'backup.failed'") == 1


def test_daily_step_runs_once_and_not_when_off(backups):
    """The daily step makes one backup a day, also when it fails, and none when switched off."""
    at_four = dt.datetime(2026, 11, 5, 4, 0)
    assert backups.step(at_four) and len(backups._client.runs) == 1
    assert not backups.step(at_four.replace(hour=5))
    backups._client.fail = True
    assert backups.step(at_four + dt.timedelta(days=1))
    assert not backups.step(at_four + dt.timedelta(days=1, hours=1))
    backups.configure("boss", False, "03:00", 5)
    assert not backups.step(at_four + dt.timedelta(days=2))
    assert len(backups._client.runs) == 2


def test_only_backup_files_can_be_downloaded(backups):
    """Only names of backups in the folder resolve to a path."""
    (backups.folder / "ow-backup-20261101-030000.tar").write_bytes(b"x")
    assert backups.path("ow-backup-20261101-030000.tar").name == "ow-backup-20261101-030000.tar"
    for name in ("../state/secret.key", "notes.txt", "ow-backup-20261102-030000.tar"):
        with pytest.raises(FileNotFoundError):
            backups.path(name)


def test_download_route_is_for_admins(client, users, service):
    """Admins can download a backup; users get 403; the download is audited."""
    service.backups.folder.mkdir(parents=True, exist_ok=True)
    (service.backups.folder / "ow-backup-20261101-030000.tar").write_bytes(b"backup")
    assert client.get("/admin/backups/ow-backup-20261101-030000.tar", follow_redirects=False)\
        .status_code == 303  # fmt: skip
    login(client, "user1")
    assert client.get("/admin/backups/ow-backup-20261101-030000.tar").status_code == 403
    client.cookies.clear()
    login(client, "boss")
    response = client.get("/admin/backups/ow-backup-20261101-030000.tar")
    assert response.status_code == 200 and response.content == b"backup"
    assert client.get("/admin/backups/secret.key").status_code == 404
    assert service.db.scalar("SELECT count(*) FROM audit WHERE action = 'backup.download'") == 1
