# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from orbitalwindow.config import Settings
from orbitalwindow.db import Database
from orbitalwindow.main import create_app
from orbitalwindow.service import Service

ORIGIN = "https://testserver"
ADDONS_DIR = Path(__file__).parent.parent / "addons"


class FakeSamba:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.passwords: dict[str, str] = {}

    def sync(self, db) -> None:
        self.calls.append(("sync",))

    def set_password(self, name: str, password: str) -> None:
        self.calls.append(("passwd", name))
        self.passwords[name] = password

    def set_enabled(self, name: str, enabled: bool) -> None:
        self.calls.append(("enabled", name, enabled))

    def remove_user(self, name: str, db) -> None:
        self.calls.append(("remove", name))


class FakeHostOps:
    def __init__(self) -> None:
        self.homes: list[tuple[str, int]] = []
        self.group_dirs = {}
        self.removed_group_dirs = []
        self.files_data = set()
        self.readme_days = []
        self.sizes = {}

    def create_home(self, name: str, uid: int) -> None:
        self.homes.append((name, uid))

    def remove_home(self, name: str) -> None:
        self.homes = [h for h in self.homes if h[0] != name]

    group_dirs: dict[str, int]
    removed_group_dirs: list[str]
    files_data: set[str]
    readme_days: list[int]
    sizes: dict[str, int]

    def create_group_dir(self, name: str, gid: int) -> None:
        self.group_dirs[name] = gid

    def remove_group_dir(self, name: str) -> None:
        self.removed_group_dirs.append(name)

    def create_files_data(self, name: str, uid: int) -> None:
        self.files_data.add(name)

    def remove_files_data(self, name: str) -> None:
        self.files_data.discard(name)

    def prepare_layout(self, service_uid: int, scratch_days: int = 30) -> None:
        self.readme_days.append(scratch_days)

    def disk_usage(self) -> dict[str, int]:
        return dict(self.sizes)

    def clean_scratch(self, days: int) -> list[str]:
        return ["./old.dat"]


class FakeFiles:
    def __init__(self) -> None:
        self.running: dict[str, list[str]] = {}
        self.recreated: list[str] = []

    def ensure(self, user, groups, *, recreate: bool = False) -> bool:
        if recreate:
            self.recreated.append(user["name"])
        self.running[user["name"]] = [g["name"] for g in groups]
        return True

    def stop(self, name: str) -> None:
        self.running.pop(name, None)

    def remove(self, name: str) -> None:
        self.running.pop(name, None)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        root=tmp_path / "ow",
        hostname="desk.test",
        admin_password=None,
        cookie_secure=True,
        addons_dir=ADDONS_DIR,
    )


@pytest.fixture
def service(settings) -> Service:
    svc = Service(settings, MagicMock(), db=Database(":memory:"), prepare_layout=False)
    svc.samba = FakeSamba()  # type: ignore[assignment]
    svc.hostops = FakeHostOps()  # type: ignore[assignment]
    svc.files = FakeFiles()  # type: ignore[assignment]
    # No file manager containers exist yet in tests.
    svc._files_exists = lambda name: name in svc.files.running  # type: ignore[method-assign]
    svc.db.execute(
        "INSERT INTO images (label, base_tag, image_ref, status, built_at, docker_id) "
        "VALUES ('Ubuntu XFCE', 'ubuntu-xfce', 'ow-desktop:ubuntu-xfce', 'ready', now(), "
        "'sha256:base1')"
    )
    return svc


@pytest.fixture
def users(service):
    """user1, user2 and an admin, all with password 'correct-horse-1'."""
    for name, admin in (("user1", False), ("user2", False), ("boss", True)):
        service.create_user(
            actor="test", name=name, display_name=name, password="correct-horse-1", is_admin=admin
        )
    return {r["name"]: r for r in service.list_users()}


@pytest.fixture
def client(service) -> TestClient:
    app = create_app(service.settings, service)
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


def login(client: TestClient, name: str, password: str = "correct-horse-1") -> str | None:
    r = client.post(
        "/auth/login",
        data={"username": name, "password": password, "next": "/"},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    return r.cookies.get("ow_session")
