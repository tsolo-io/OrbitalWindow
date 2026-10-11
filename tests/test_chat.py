# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import docker
import pytest
from conftest import login

from orbitalwindow.chat import CONTAINER, LABEL_SPEC, Chat, chat_spec
from orbitalwindow.config import Settings

SETTINGS = Settings(root="/srv/ow", hostname="desk.test")


def test_spec_signs_in_through_orbitalwindow_headers():
    """Open WebUI trusts the forwardAuth headers, has no login form and talks to the session."""
    spec = chat_spec(SETTINGS, "ow-key", "secret", "desk.test")
    env = spec["environment"]
    assert env["WEBUI_AUTH_TRUSTED_EMAIL_HEADER"] == "X-OW-Email"
    assert env["WEBUI_AUTH_TRUSTED_NAME_HEADER"] == "X-OW-Name"
    assert env["WEBUI_AUTH_TRUSTED_ROLE_HEADER"] == "X-OW-Role"
    assert env["ENABLE_LOGIN_FORM"] == "false" and env["ENABLE_SIGNUP"] == "false"
    assert env["OPENAI_API_BASE_URL"] == "http://ow-ai-model:8000/v1"
    assert env["OPENAI_API_KEY"] == "ow-key" and env["ENABLE_OLLAMA_API"] == "false"
    assert env["ENABLE_API_KEYS"] == "true" and env["USER_PERMISSIONS_FEATURES_API_KEYS"] == "true"
    assert env["OFFLINE_MODE"] == "true" and env["ENABLE_PERSISTENT_CONFIG"] == "false"
    assert env["WEBUI_URL"] == env["CORS_ALLOW_ORIGIN"] == "https://desk.test:8443"
    assert env["DATA_DIR"] == "/data"
    assert spec["volumes"] == {"/srv/ow/state/openwebui": {"bind": "/data", "mode": "rw"}}
    assert spec["network"] == "ow-ai"


def test_spec_routes_browser_and_api_key_requests():
    """Browsers go through forwardAuth; requests with an Open WebUI key skip it."""
    labels = chat_spec(SETTINGS, "k", "s", "desk.test")["labels"]
    assert labels["traefik.http.routers.ow-chat.entrypoints"] == "ai"
    assert labels["traefik.http.routers.ow-chat.middlewares"] == "ow-auth-ai@docker"
    assert (
        "HeaderRegexp(`Authorization`, `^Bearer sk-`)"
        in labels["traefik.http.routers.ow-chat-api.rule"]
    )
    assert labels["traefik.http.routers.ow-chat-api.middlewares"] == "ow-chat-strip@docker"
    assert int(labels["traefik.http.routers.ow-chat-api.priority"]) > 1


def test_spec_hash_changes_with_the_key():
    """A new API key (or host name) changes the spec hash, so the container is recreated."""
    first = chat_spec(SETTINGS, "k1", "s", "desk.test")["labels"][LABEL_SPEC]
    assert chat_spec(SETTINGS, "k1", "s", "desk.test")["labels"][LABEL_SPEC] == first
    assert chat_spec(SETTINGS, "k2", "s", "desk.test")["labels"][LABEL_SPEC] != first
    assert chat_spec(SETTINGS, "k1", "s", "other.test")["labels"][LABEL_SPEC] != first


class FakeContainer:
    def __init__(self, labels, status="running"):
        """Remember labels and status."""
        self.labels, self.status, self.removed, self.started = labels, status, False, False
        self.attrs = {"State": {"Health": {"Status": "healthy"}}}

    def reload(self):
        """Keep the scripted state."""

    def remove(self, force=False):
        """Mark as removed."""
        self.removed = True

    def start(self):
        """Mark as started."""
        self.started, self.status = True, "running"


class FakeClient:
    def __init__(self):
        """Start without containers; the session network exists."""
        self.current, self.runs, self.pulls = None, [], []
        self.network = SimpleNamespace(
            name="ow-ai", containers=[], connected=[], reload=lambda: None
        )
        self.network.connect = self.network.connected.append
        self.containers = SimpleNamespace(get=self._get, run=self._run)
        self.networks = SimpleNamespace(list=lambda names=None: [self.network])
        self.images = SimpleNamespace(get=self._image, pull=self.pulls.append)

    def _get(self, name):
        """Return the chat container or raise NotFound."""
        if self.current is None or self.current.removed:
            raise docker.errors.NotFound("none")
        return self.current

    def _image(self, image):
        """Pretend the image is missing."""
        raise docker.errors.ImageNotFound("missing")

    def _run(self, image, **kwargs):
        """Record the run."""
        self.runs.append((image, kwargs))
        self.current = FakeContainer(kwargs["labels"])
        return self.current


@pytest.fixture
def chat(tmp_path):
    """A Chat with a fake Docker client and its data in tmp_path."""
    return Chat(FakeClient(), Settings(root=tmp_path, hostname="desk.test"))


def test_chat_follows_the_session(chat, tmp_path):
    """Started (image pulled, Traefik connected) while wanted, kept when unchanged, else removed."""
    client = chat._client
    chat.ensure(True, "k1", "desk.test")
    assert client.pulls == ["ghcr.io/open-webui/open-webui:v0.11.4"] and len(client.runs) == 1
    assert client.network.connected == ["ow-traefik"]
    assert (tmp_path / "state" / "openwebui").is_dir()
    secret = (tmp_path / "state" / "openwebui_secret").read_text().strip()
    assert client.runs[0][1]["environment"]["WEBUI_SECRET_KEY"] == secret
    chat.ensure(True, "k1", "desk.test")
    assert len(client.runs) == 1  # unchanged: kept
    chat.ensure(True, "k2", "desk.test")
    assert len(client.runs) == 2  # new key: recreated, same secret
    assert client.runs[1][1]["environment"]["WEBUI_SECRET_KEY"] == secret
    assert chat.ready()
    chat.ensure(False, "k2", "desk.test")
    assert client.current.removed and not chat.ready()
    assert client.runs[0][1]["name"] == CONTAINER


def test_stopped_chat_container_is_started_again(chat):
    """An unchanged but stopped container is started instead of recreated."""
    chat.ensure(True, "k", "desk.test")
    chat._client.current.status = "exited"
    chat.ensure(True, "k", "desk.test")
    assert chat._client.current.started and len(chat._client.runs) == 1


# --- forwardAuth for the chat -----------------------------------------------------------------


def auth_ai(client, cookie, host="desk.test:8443"):
    """Call the chat forwardAuth endpoint as Traefik would."""
    client.cookies.clear()
    if cookie:
        client.cookies.set("ow_session", cookie)
    headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": host, "X-Forwarded-Uri": "/"}
    return client.get("/auth/ai", headers=headers, follow_redirects=False)


def test_chat_auth_without_session_goes_to_login_on_the_main_port(client, users):
    """Without a session the chat sends people to OrbitalWindow's login page (port 443)."""
    r = auth_ai(client, None)
    assert r.status_code == 302 and r.headers["location"] == "https://desk.test/login?next=/"
    assert auth_ai(client, None, host="evil.example/x").status_code == 401


def test_chat_auth_passes_user_and_role(client, users, service):
    """Users get their e-mail, name and role; admins become Open WebUI admins."""
    service.set_display_name("test", users["user1"]["id"], "Ada Lovelace")
    r = auth_ai(client, login(client, "user1"))
    assert r.status_code == 200
    assert r.headers["x-ow-email"] == "user1@orbitalwindow.local"
    assert r.headers["x-ow-name"] == "Ada%20Lovelace"
    assert r.headers["x-ow-role"] == "user"
    r = auth_ai(client, login(client, "boss"))
    assert r.headers["x-ow-role"] == "admin"


def test_chat_url_and_readiness(service):
    """The chat is on its own port and ready only with a running session and Open WebUI."""
    assert service.chat_url() == "https://desk.test:8443/"
    service.ai.state = lambda: SimpleNamespace(status="starting")
    service.chat.ready = lambda: True
    assert not service.chat_ready()
    service.ai.state = lambda: SimpleNamespace(status="running")
    assert service.chat_ready()
