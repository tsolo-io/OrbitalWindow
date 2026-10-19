# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import docker
import pytest
from conftest import login

from orbitalwindow.codeserver import (
    CodeServers,
    CodeState,
    build_code_spec,
    code_path,
    image_tag,
)
from orbitalwindow.users import ValidationError

USER = {"name": "user1", "uid_gid": 2001, "cpus": 2, "mem_gb": 4}
IMAGE_CONTEXT = Path(__file__).parent.parent / "deploy" / "image"
GROUPS = [{"name": "labx", "gid": 3001}]


def test_spec_mounts_the_home_like_a_desktop(settings):
    """The home is /config, group folders and scratch are mounted, shared data read-only."""
    spec = build_code_spec(USER, GROUPS, settings, {"MY_VAR": "1", "PATH": "/evil"})
    kwargs = spec.kwargs
    root = settings.root
    assert kwargs["volumes"][str(root / "users" / "user1")] == {"bind": "/config", "mode": "rw"}
    assert kwargs["volumes"][str(root / "groups" / "labx")]["bind"] == "/groups/labx"
    assert kwargs["volumes"][str(root / "shared" / "data")]["mode"] == "ro"
    env = kwargs["environment"]
    assert (env["PUID"], env["PGID"], env["OW_GUI"]) == ("2001", "2001", "false")
    assert env["OW_GROUPS"] == "labx:3001" and env["MY_VAR"] == "1" and "PATH" not in env
    assert kwargs["nano_cpus"] == 2 * 10**9 and kwargs["mem_limit"] == 4 * 1024**3
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert spec.network == "ow-cnet-user1" and spec.name == "ow-code-user1"


def test_spec_routes_only_the_owners_path_through_forward_auth(settings):
    """Traefik routes /code/<user>/ with forwardAuth, then strips the prefix for code-server."""
    labels = build_code_spec(USER, [], settings).kwargs["labels"]
    router = "traefik.http.routers.ow-code-user1"
    assert labels[f"{router}.rule"] == "Path(`/code/user1`) || PathPrefix(`/code/user1/`)"
    assert labels[f"{router}.middlewares"] == (
        "ow-code-slash@docker,ow-auth-code@docker,ow-code-user1-strip@docker"
    )
    strip = "traefik.http.middlewares.ow-code-user1-strip.stripprefix.prefixes"
    assert labels[strip] == "/code/user1"
    assert labels["traefik.docker.network"] == "ow-cnet-user1"
    assert code_path("user1") == "/code/user1/"


def test_image_tag_follows_the_build_files(tmp_path):
    """The image is rebuilt when its files or the code-server version change."""
    (tmp_path / "code").mkdir()
    for name in ("Dockerfile.code", "code/run"):
        (tmp_path / name).write_text("a")
    (tmp_path / "root/custom-cont-init.d").mkdir(parents=True)
    (tmp_path / "root/usr/local/bin").mkdir(parents=True)
    (tmp_path / "root/custom-cont-init.d/50-ow-setup").write_text("a")
    (tmp_path / "root/usr/local/bin/ow-pkg").write_text("a")
    first = image_tag(tmp_path, "code-server:1")
    assert first.startswith("ow-code:") and first == image_tag(tmp_path, "code-server:1")
    assert image_tag(tmp_path, "code-server:2") != first
    (tmp_path / "code/run").write_text("b")
    assert image_tag(tmp_path, "code-server:1") != first


class FakeContainer:
    def __init__(self, status="running", health="healthy"):
        """Remember the scripted state."""
        self.attrs = {"State": {"Status": status, "Health": {"Status": health}, "ExitCode": 1}}
        self.removed = self.started = False

    def start(self):
        """Mark it started."""
        self.started = True

    def remove(self, force=False):
        """Mark it removed."""
        self.removed = True

    def logs(self, tail=10):
        """Return a log line."""
        return b"boom"


class FakeContainers:
    def __init__(self):
        """Start without a container."""
        self.current = None
        self.created = []

    def get(self, name):
        """Return the container, or raise NotFound."""
        if self.current is None or self.current.removed:
            raise docker.errors.NotFound("none")
        return self.current

    def create(self, image, **kwargs):
        """Record the creation and return a running container."""
        self.created.append((image, kwargs))
        self.current = FakeContainer()
        return self.current


@pytest.fixture
def code(settings):
    """CodeServers with a fake Docker client whose image does not exist yet."""
    images = MagicMock()
    images.get.side_effect = docker.errors.ImageNotFound("no")
    network = MagicMock()
    network.name = "ow-cnet-user1"
    network.containers = []
    client = SimpleNamespace(containers=FakeContainers(), images=images, networks=MagicMock())
    client.networks.list.return_value = []
    client.networks.create.return_value = network
    return CodeServers(client, settings.model_copy(update={"image_context": IMAGE_CONTEXT}))


def test_start_builds_the_image_then_runs_the_container(code):
    """The first start builds ow-code:<hash>, joins Traefik to the user's network and runs."""
    assert code.state("user1") == (CodeState.STOPPED, "")
    code.start(USER, GROUPS, {}, wait=True)
    image, kwargs = code._client.containers.created[0]
    assert image.startswith("ow-code:") and kwargs["network"] == "ow-cnet-user1"
    build = code._client.images.build.call_args.kwargs
    assert build["dockerfile"] == "Dockerfile.code"
    assert build["buildargs"]["BASE_IMAGE"].startswith("lscr.io/linuxserver/code-server:")
    code._client.networks.create.return_value.connect.assert_called_once_with("ow-traefik")
    assert code.state("user1") == (CodeState.RUNNING, "")
    code._client.containers.current.attrs["State"]["Health"]["Status"] = "starting"
    assert code.state("user1")[0] is CodeState.STARTING
    code.stop("user1")
    assert code.state("user1") == (CodeState.STOPPED, "")


def test_failures_are_shown(code):
    """A failed build or an exited container is an error with the reason."""
    code._client.images.build.side_effect = docker.errors.BuildError("no network", [])
    code.start(USER, [], {}, wait=True)
    state, error = code.state("user1")
    assert state is CodeState.ERROR and "no network" in error
    code._client.images.build.side_effect = None
    code.start(USER, [], {}, wait=True)
    code._client.containers.current.attrs["State"]["Status"] = "exited"
    assert code.state("user1") == (CodeState.ERROR, "boom")


def test_service_start_stop_and_disable(service, users, monkeypatch):
    """Users start and stop their own VS Code; disabling a user stops it; both are audited."""
    calls = []
    monkeypatch.setattr(service.code, "start", lambda user, groups, env: calls.append(user["name"]))
    monkeypatch.setattr(service.code, "stop", lambda name: calls.append(f"stop {name}"))
    user1 = users["user1"]["id"]
    service.start_code("user1", user1)
    service.stop_code("user1", user1)
    service.set_disabled("boss", users["boss"]["id"], user1, True)
    assert calls == ["user1", "stop user1", "stop user1"]
    with pytest.raises(ValidationError, match="disabled"):
        service.start_code("user1", user1)
    actions = service.db.all("SELECT action FROM audit WHERE action LIKE 'code.%' ORDER BY rowid")
    assert [row["action"] for row in actions] == ["code.start", "code.stop"]


def test_forward_auth_lets_only_the_owner_in(client, users):
    """/code/<user>/ is the owner's only: others get 403, no session gets a login."""
    headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "desk.test"}

    def check(uri, cookie):
        """Call the VS Code forwardAuth endpoint as Traefik would.

        Args:
            uri: Value for X-Forwarded-Uri.
            cookie: Session cookie, or None.

        Returns:
            The response.
        """
        client.cookies.clear()
        if cookie:
            client.cookies.set("ow_session", cookie)
        uri_headers = headers | {"X-Forwarded-Uri": uri}
        return client.get("/auth/code", headers=uri_headers, follow_redirects=False)

    owner = login(client, "user1")
    allowed = check("/code/user1/static/x.js", owner)
    assert allowed.status_code == 200 and allowed.headers["x-ow-user"] == "user1"
    assert check("/code/user2/", owner).status_code == 403
    assert check("/code/user1/", login(client, "boss")).status_code == 403
    assert check("/code/user1/", None).status_code == 302
    assert check("/desktop/user1/", owner).status_code == 403
