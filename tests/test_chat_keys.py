# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Users' chat API keys on the dashboard (#61)."""

import json
import ssl

import httpx2
import pytest

from orbitalwindow.chat import ChatKeyError, ChatKeys, trusted_headers
from orbitalwindow.users import ValidationError


class FakeOpenWebUI:
    """Answers like Open WebUI: trusted-header sign-in, then the signed-in user's key."""

    def __init__(self):
        """Start with no keys."""
        self.keys = {}
        self.signins = []

    def __call__(self, request):
        """Answer one request.

        Args:
            request: The HTTP request.

        Returns:
            The response.
        """
        if request.url.path == "/api/v1/auths/signin":
            email = request.headers["x-ow-email"]
            self.signins.append((email, request.headers["x-ow-role"]))
            return httpx2.Response(200, json={"token": f"tok-{email}", "role": "user"})
        email = request.headers["authorization"].removeprefix("Bearer tok-")
        if request.method == "GET":
            key = self.keys.get(email)
            return httpx2.Response(200 if key else 404, json={"api_key": key} if key else {})
        self.keys[email] = f"sk-{email}-{len(self.keys)}"
        return httpx2.Response(200, json={"api_key": self.keys[email]})


@pytest.fixture
def webui():
    """A fake Open WebUI and a ChatKeys client talking to it."""
    fake = FakeOpenWebUI()
    client = httpx2.Client(base_url="http://chat", transport=httpx2.MockTransport(fake))
    return fake, ChatKeys(client=client)


def test_headers_are_those_forward_auth_sends():
    """The key client signs users in exactly as the browser's forwardAuth does."""
    assert trusted_headers("jsmith", "Jo Smith", False) == {
        "X-OW-Email": "jsmith@orbitalwindow.local",
        "X-OW-Name": "Jo%20Smith",
        "X-OW-Role": "user",
    }


def test_get_create_and_renew(webui):
    """No key until one is created; renewing replaces it; admins sign in as admins."""
    fake, keys = webui
    assert keys.get("jsmith", "Jo", False) is None
    first = keys.get("jsmith", "Jo", False, create=True)
    assert first.startswith("sk-") and keys.get("jsmith", "Jo", False) == first
    assert keys.renew("jsmith", "Jo", False) != first
    keys.get("boss", "Boss", True)
    assert ("boss@orbitalwindow.local", "admin") in fake.signins


def test_open_webui_down():
    """A chat that can't be reached is a ChatKeyError, not a crash."""

    def refuse(request):
        """Refuse every connection.

        Args:
            request: The HTTP request.

        Raises:
            httpx2.ConnectError: Always.
        """
        raise httpx2.ConnectError("refused", request=request)

    transport = httpx2.MockTransport(refuse)
    keys = ChatKeys(client=httpx2.Client(base_url="http://chat", transport=transport))
    with pytest.raises(ChatKeyError, match="did not answer"):
        keys.get("jsmith", "Jo", False)


def test_service_shows_and_creates_keys(service, users, webui, monkeypatch):
    """The dashboard gets address, model and key; creating and renewing are audited."""
    _, keys = webui
    service.chat_keys = keys
    user1 = users["user1"]["id"]
    monkeypatch.setattr(service, "chat_ready", lambda: False)
    access = service.chat_access(user1)
    assert access["url"] == "https://desk.test:8443/api" and access["key"] is None
    assert "not running" in access["error"]
    with pytest.raises(ValidationError, match="not running"):
        service.create_chat_key("user1", user1)
    monkeypatch.setattr(service, "chat_ready", lambda: True)
    assert service.chat_access(user1)["key"] is None and not service.chat_access(user1)["error"]
    key = service.create_chat_key("user1", user1)
    assert service.chat_access(user1)["key"] == key
    assert service.create_chat_key("user1", user1, renew=True) != key
    actions = service.db.all(
        "SELECT action FROM audit WHERE action LIKE 'chat.key.%' ORDER BY rowid"
    )
    assert [row["action"] for row in actions] == ["chat.key.create", "chat.key.renew"]


def test_certificate_download(client, monkeypatch):
    """/orbitalwindow.crt serves Traefik's certificate as a file, without a login."""
    pem = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
    monkeypatch.setattr(ssl, "get_server_certificate", lambda address, timeout: pem)
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

    monkeypatch.setattr(ssl, "get_server_certificate", down)
    assert client.get("/orbitalwindow.crt").status_code == 503


def test_help_page_fills_in_the_running_model():
    """The AI page's examples get the chat address, the model and its context."""
    from pathlib import Path

    from orbitalwindow.help import list_pages, render

    folder = Path(__file__).parent.parent / "docs" / "content" / "user"
    page = next(p for p in list_pages(folder) if p.slug == "ai")
    text = render(page, {"chat_api": "https://h:8443/api", "model": "m/x", "context": "131072"})
    assert '"baseURL": "https://h:8443/api"' in text and '"m/x": {' in text
    assert '"context": 131072' in text and "{model}" not in text
    json.loads(text.split("```json\n")[1].split("```")[0])  # the OpenCode example stays valid JSON
