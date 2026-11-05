# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import itertools
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
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
        """Record a Samba sync instead of rewriting the users file.

        Args:
            db: Database the real implementation would read users and groups from.
        """
        self.calls.append(("sync",))

    def set_password(self, name: str, password: str) -> None:
        """Record a Samba password change and remember the new password.

        Args:
            name: User name.
            password: New plain-text password.
        """
        self.calls.append(("passwd", name))
        self.passwords[name] = password

    def set_enabled(self, name: str, enabled: bool) -> None:
        """Record enabling or disabling a Samba account.

        Args:
            name: User name.
            enabled: Whether the account should be enabled.
        """
        self.calls.append(("enabled", name, enabled))

    def remove_user(self, name: str, db) -> None:
        """Record removing a user from Samba.

        Args:
            name: User name.
            db: Database the real implementation would resync from.
        """
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
        """Record the creation of a user's home folder.

        Args:
            name: User name.
            uid: Owner UID/GID of the folder.
        """
        self.homes.append((name, uid))

    def remove_home(self, name: str) -> None:
        """Forget a previously created home folder.

        Args:
            name: User name.
        """
        self.homes = [h for h in self.homes if h[0] != name]

    group_dirs: dict[str, int]
    removed_group_dirs: list[str]
    files_data: set[str]
    readme_days: list[int]
    sizes: dict[str, int]

    def create_group_dir(self, name: str, gid: int) -> None:
        """Record the creation of a group folder.

        Args:
            name: Group name.
            gid: Owner GID of the folder.
        """
        self.group_dirs[name] = gid

    def remove_group_dir(self, name: str) -> None:
        """Record the removal of a group folder.

        Args:
            name: Group name.
        """
        self.removed_group_dirs.append(name)

    def create_files_data(self, name: str, uid: int) -> None:
        """Record the creation of a user's file manager data folder.

        Args:
            name: User name.
            uid: Owner UID of the folder.
        """
        self.files_data.add(name)

    def remove_files_data(self, name: str) -> None:
        """Forget a user's file manager data folder.

        Args:
            name: User name.
        """
        self.files_data.discard(name)

    def prepare_layout(self, service_uid: int, scratch_days: int = 30) -> None:
        """Record the scratch retention written into the scratch README.

        Args:
            service_uid: UID the real implementation would own service folders with.
            scratch_days: Scratch retention in days.
        """
        self.readme_days.append(scratch_days)

    def disk_usage(self) -> dict[str, int]:
        """Return the canned folder sizes.

        Returns:
            A copy of ``sizes``: bytes per folder path relative to the root.
        """
        return dict(self.sizes)

    def clean_scratch(self, days: int) -> list[str]:
        """Pretend to clean scratch and report one removed file.

        Args:
            days: Retention in days (ignored).

        Returns:
            A fixed list with one removed path.
        """
        return ["./old.dat"]


class FakeFiles:
    def __init__(self) -> None:
        self.running: dict[str, list[str]] = {}
        self.recreated: list[str] = []

    def ensure(self, user, groups, *, recreate: bool = False) -> bool:
        """Record that a user's file manager runs with the given groups.

        Args:
            user: User row.
            groups: Group rows the user is a member of.
            recreate: Whether the container would be recreated.

        Returns:
            Always True.
        """
        if recreate:
            self.recreated.append(user["name"])
        self.running[user["name"]] = [g["name"] for g in groups]
        return True

    def stop(self, name: str) -> None:
        """Mark a user's file manager as stopped.

        Args:
            name: User name.
        """
        self.running.pop(name, None)

    def remove(self, name: str) -> None:
        """Mark a user's file manager as removed.

        Args:
            name: User name.
        """
        self.running.pop(name, None)


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Return settings rooted in a temporary folder."""
    return Settings(
        root=tmp_path / "ow",
        hostname="desk.test",
        admin_password=None,
        cookie_secure=True,
        addons_dir=ADDONS_DIR,
    )


TEST_DB_IMAGE = "postgres:18.6-alpine3.24"  # as deploy/compose.yaml
_test_db_numbers = itertools.count(1)


def _start_test_server():
    """Start a throwaway PostgreSQL container for the tests (data on tmpfs, no fsync).

    Returns:
        The container and the superuser's URL of its ``postgres`` database.
    """
    import docker

    client = docker.from_env()
    container = client.containers.run(
        TEST_DB_IMAGE,
        ["-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"],
        detach=True,
        auto_remove=True,
        environment={"POSTGRES_PASSWORD": "test"},
        tmpfs={"/var/lib/postgresql": ""},
        ports={"5432/tcp": ("127.0.0.1", None)},
        labels={"ow.test": "true"},
    )
    container.reload()
    port = container.attrs["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
    url = f"postgresql://postgres:test@127.0.0.1:{port}/postgres"
    deadline = time.monotonic() + 60
    while True:
        try:
            psycopg.connect(url, connect_timeout=2).close()
            # The entrypoint restarts the server once after initdb; wait for the final one.
            if (
                "database system is ready to accept connections"
                in container.logs().decode().split("PostgreSQL init process complete")[-1]
            ):
                return container, url
        except psycopg.OperationalError:
            pass
        if time.monotonic() > deadline:
            container.stop()
            raise RuntimeError("the test PostgreSQL server did not start")
        time.sleep(0.3)


@pytest.fixture(scope="session")
def pg_server():
    """Return the superuser URL of a test server's ``postgres`` database, with a template.

    ``OW_TEST_DATABASE_URL`` (CI) names a server to use; otherwise a container is started.
    The template database ``ow_template`` holds the migrated schema, so each test's database is a
    quick copy of it.
    """
    container = None
    url = os.environ.get("OW_TEST_DATABASE_URL")
    if not url:
        container, url = _start_test_server()
    with psycopg.connect(url, autocommit=True) as con:
        con.execute("DROP DATABASE IF EXISTS ow_template WITH (FORCE)")
        con.execute("CREATE DATABASE ow_template")
    Database(url.rsplit("/", 1)[0] + "/ow_template").close()
    yield url
    if container is not None:
        container.stop(timeout=2)


@pytest.fixture
def db_url(pg_server):
    """Return the URL of a new, migrated, empty database for one test."""
    name = f"ow_test_{os.getpid()}_{next(_test_db_numbers)}"
    with psycopg.connect(pg_server, autocommit=True) as con:
        con.execute(f"CREATE DATABASE {name} TEMPLATE ow_template")
    yield pg_server.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(pg_server, autocommit=True) as con:
        con.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def database(db_url) -> Database:
    """Return a connected, migrated, empty database for one test."""
    db = Database(db_url)
    yield db
    db.close()


@pytest.fixture
def service(settings, database, monkeypatch) -> Service:
    """Return a Service with an empty database, fakes and one ready base image.

    The other programs' databases (Open WebUI's) are not made: there is no ``ow-db`` here.
    """
    monkeypatch.setattr("orbitalwindow.chat.pg.ensure_database", lambda *args: None)
    svc = Service(settings, MagicMock(), db=database, prepare_layout=False)
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
    """Yield a TestClient for the API app built on the test service."""
    app = create_app(service.settings, service)
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


def login(client: TestClient, name: str, password: str = "correct-horse-1") -> str | None:
    """Log in through the API and return the session cookie.

    Args:
        client: Test client to post with.
        name: User name.
        password: Password to log in with.

    Returns:
        The ``ow_session`` cookie value, or None if the login failed.
    """
    r = client.post(
        "/auth/login",
        data={"username": name, "password": password, "next": "/"},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    return r.cookies.get("ow_session")
