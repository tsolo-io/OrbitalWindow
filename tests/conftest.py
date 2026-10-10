# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from orbitalwindow.config import Settings
from orbitalwindow.db import Database
from orbitalwindow.main import create_app
from orbitalwindow.service import Service

ORIGIN = "https://testserver"


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

    def create_home(self, name: str, uid: int) -> None:
        self.homes.append((name, uid))

    def remove_home(self, name: str) -> None:
        self.homes = [h for h in self.homes if h[0] != name]


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        root=tmp_path / "ow", hostname="desk.test", admin_password=None, cookie_secure=True
    )


@pytest.fixture
def service(settings) -> Service:
    svc = Service(settings, MagicMock(), db=Database(":memory:"), prepare_layout=False)
    svc.samba = FakeSamba()  # type: ignore[assignment]
    svc.hostops = FakeHostOps()  # type: ignore[assignment]
    svc.db.execute(
        "INSERT INTO images (label, base_tag, image_ref, status) "
        "VALUES ('Ubuntu XFCE', 'ubuntu-xfce', 'ow-desktop:ubuntu-xfce', 'ready')"
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
