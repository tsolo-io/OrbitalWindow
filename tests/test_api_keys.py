# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Users' API keys for the LLM gateway on the dashboard (#61, #79), and the AI help page."""

import json
from pathlib import Path

import pytest

from orbitalwindow import api
from orbitalwindow.chat import trusted_headers
from orbitalwindow.gateway import GatewayError
from orbitalwindow.users import ValidationError


class FakeGateway:
    """Records key changes like the gateway; ``down`` makes every call fail."""

    def __init__(self):
        """Start with no keys; ready."""
        self.keys, self.down, self.up = {}, False, True

    def _check(self):
        """Fail like a gateway that can't be reached."""
        if self.down:
            raise GatewayError("The LLM gateway can't be reached: refused")

    def ready(self):
        """Tell whether the gateway is up."""
        return self.up

    def add_key(self, name, key, blocked=False):
        """Give the user this key."""
        self._check()
        self.keys[name] = {"key": key, "blocked": blocked}

    def set_blocked(self, key, blocked):
        """Block or unblock a key."""
        self._check()
        next(v for v in self.keys.values() if v["key"] == key)["blocked"] = blocked

    def delete_key(self, name):
        """Delete the user's key."""
        self._check()
        self.keys.pop(name, None)


@pytest.fixture
def keys(service):
    """The service with a fake gateway."""
    service.gateway = FakeGateway()
    return service.gateway


def test_headers_are_those_forward_auth_sends():
    """The chat's forwardAuth signs users in with these headers."""
    assert trusted_headers("jsmith", "Jo Smith", False) == {
        "X-OW-Email": "jsmith@orbitalwindow.local",
        "X-OW-Name": "Jo%20Smith",
        "X-OW-Role": "user",
    }


def test_users_create_show_and_renew_their_key(service, users, keys):
    """No key until one is made; it is shown again; renewing replaces it; all audited."""
    user1 = users["user1"]["id"]
    access = service.api_access(user1)
    assert access["url"] == "https://desk.test:8443/v1" and access["key"] is None
    assert access["fim"] is None and access["error"] == ""
    key = service.create_api_key("user1", user1)
    assert key.startswith("sk-ow-") and keys.keys["user1"]["key"] == key
    assert service.api_access(user1)["key"] == key
    assert service.create_api_key("user1", user1) == key  # already has one: kept
    new = service.create_api_key("user1", user1, renew=True)
    assert new != key and keys.keys["user1"]["key"] == new
    assert service.api_access(user1)["key"] == new
    actions = service.db.all("SELECT action FROM audit WHERE action LIKE 'api.key.%' ORDER BY id")
    assert [row["action"] for row in actions] == ["api.key.create", "api.key.renew"]


def test_no_key_while_the_gateway_is_down(service, users, keys):
    """A key is only stored once the gateway has it."""
    user1 = users["user1"]["id"]
    keys.down, keys.up = True, False
    assert "starting" in service.api_access(user1)["error"]
    with pytest.raises(ValidationError, match="can't be reached"):
        service.create_api_key("user1", user1)
    assert service.api_access(user1)["key"] is None


def test_disabling_blocks_and_deleting_removes_the_key(service, users, keys):
    """The key follows the account; a gateway that is down is a warning (reconcile fixes it)."""
    user1 = users["user1"]["id"]
    service.create_api_key("user1", user1)
    service.set_disabled("boss", users["boss"]["id"], user1, True)
    assert keys.keys["user1"]["blocked"] is True
    service.set_disabled("boss", users["boss"]["id"], user1, False)
    assert keys.keys["user1"]["blocked"] is False
    keys.down = True
    result = service.set_disabled("boss", users["boss"]["id"], user1, True)
    assert any("API key was not updated" in warning for warning in result.warnings)
    keys.down = False
    service.delete_user("boss", users["boss"]["id"], user1)
    assert "user1" not in keys.keys
    assert service.db.scalar("SELECT count(*) FROM llm_keys") == 0


def test_sync_gateway_gives_it_the_session_model_and_the_keys(service, users, monkeypatch):
    """The reconcile step runs the gateway, gives it the running model and every user's key."""
    calls = []
    gateway = service.gateway
    monkeypatch.setattr(gateway, "ensure", lambda: calls.append("ensure"))
    monkeypatch.setattr(gateway, "ready", lambda: True)
    monkeypatch.setattr(gateway, "sync_models", lambda wanted: calls.append(wanted))
    monkeypatch.setattr(gateway, "sync_keys", lambda rows: calls.append(rows))
    monkeypatch.setattr(gateway, "add_key", lambda *args: None)
    service.create_api_key("user1", users["user1"]["id"])
    service.sync_gateway()
    assert calls[0] == "ensure" and calls[1] == []  # no session: no model
    assert [(row["name"], row["disabled"]) for row in calls[2]] == [("user1", False)]


def test_certificate_download(client, monkeypatch):
    """/orbitalwindow.crt serves the CA of Traefik's certificate as a file, without a login."""
    pem = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
    monkeypatch.setattr(api, "served_ca", lambda address, timeout: pem)
    response = client.get("/orbitalwindow.crt")
    assert response.status_code == 200 and response.text == pem
    assert "orbitalwindow.crt" in response.headers["content-disposition"]

    def down(address, timeout):
        """Fail like a stopped Traefik.

        Args:
            address: Host and port.
            timeout: Seconds.

        Raises:
            OSError: Always.
        """
        raise OSError("refused")

    monkeypatch.setattr(api, "served_ca", down)
    assert client.get("/orbitalwindow.crt").status_code == 503


def test_help_page_fills_in_the_running_model():
    """The AI page's examples get the API address, the model and its context."""
    from orbitalwindow.help import list_pages, render

    folder = Path(__file__).parent.parent / "docs" / "content" / "user"
    page = next(p for p in list_pages(folder) if p.slug == "ai")
    text = render(page, {"api_url": "https://h:8443/v1", "model": "m/x", "context": "131072"})
    assert '"baseURL": "https://h:8443/v1"' in text and '"m/x": {' in text
    assert '"context": 131072' in text and "{model}" not in text and "{api_url}" not in text
    json.loads(text.split("```json\n")[1].split("```")[0])  # the OpenCode example stays valid JSON
