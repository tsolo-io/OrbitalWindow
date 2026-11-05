# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The LLM gateway (#79, ADR 0045)."""

import json
from types import SimpleNamespace

import docker
import httpx2
import pytest

from orbitalwindow import gateway as gw
from orbitalwindow.config import Settings
from orbitalwindow.gateway import Gateway, GatewayError, gateway_spec, session_model


class FakeLiteLLM:
    """Answers the admin API like LiteLLM: keys by value and alias, models by id."""

    def __init__(self):
        """Start without keys or models."""
        self.keys, self.models, self.calls = {}, {}, []

    def __call__(self, request):
        """Answer one request.

        Args:
            request: The HTTP request.

        Returns:
            The response.
        """
        assert request.headers["authorization"].startswith("Bearer sk-")
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        self.calls.append(path)
        if path == "/key/generate":
            if body["key"] in self.keys:
                return httpx2.Response(400, json={"error": "duplicate"})
            self.keys[body["key"]] = {"key_alias": body["key_alias"], "blocked": None}
            return httpx2.Response(200, json={"key": body["key"]})
        if path == "/key/delete":
            aliases = set(body.get("key_aliases", []))
            gone = [k for k, v in self.keys.items() if v["key_alias"] in aliases]
            for key in gone:
                del self.keys[key]
            return httpx2.Response(200, json={"deleted_keys": gone})
        if path in ("/key/block", "/key/unblock"):
            self.keys[body["key"]]["blocked"] = path == "/key/block"
            return httpx2.Response(200, json={})
        if path == "/key/info":
            info = self.keys.get(request.url.params["key"])
            return httpx2.Response(404 if info is None else 200, json={"info": info or {}})
        if path == "/key/list":
            size, page = int(request.url.params["size"]), int(request.url.params["page"])
            if size > 100:  # as LiteLLM does
                return httpx2.Response(422, json={"detail": "size must be <= 100"})
            rows = list(self.keys.values())[(page - 1) * size : page * size]
            return httpx2.Response(200, json={"keys": rows})
        if path == "/model/info":
            if not self.models:  # as LiteLLM does
                return httpx2.Response(500, json={"detail": {"error": "LLM Model List not loaded"}})
            rows = [{"model_name": m["model_name"], "model_info": m["model_info"]}
                    for m in self.models.values()]  # fmt: skip
            return httpx2.Response(200, json={"data": rows})
        if path == "/model/new":
            self.models[body["model_info"]["id"]] = body
            return httpx2.Response(200, json={})
        if path == "/model/delete":
            del self.models[body["id"]]
            return httpx2.Response(200, json={})
        return httpx2.Response(404)


@pytest.fixture
def litellm(tmp_path):
    """A Gateway whose admin API is a fake LiteLLM."""
    fake = FakeLiteLLM()
    gateway = Gateway(SimpleNamespace(), Settings(root=tmp_path, hostname="desk.test"))
    gateway._http = httpx2.Client(base_url="http://gw", transport=httpx2.MockTransport(fake))
    return fake, gateway


def test_only_the_openai_endpoints_are_routed(tmp_path):
    """Traefik passes chat, completions, embeddings and models on :8443, not LiteLLM's admin API."""
    spec = gateway_spec(Settings(root=tmp_path), "sk-master", "sk-salt", "postgresql://x")
    labels = spec["labels"]
    rule = labels["traefik.http.routers.ow-llm.rule"]
    for path in ("/v1/models", "/v1/chat/completions", "/v1/completions", "/v1/embeddings"):
        assert f"`{path}`" in rule
    assert "/key" not in rule and "/ui" not in rule and "PathPrefix(`/v1`)" not in rule
    assert labels["traefik.http.routers.ow-llm.entrypoints"] == "ai"
    env = spec["environment"]
    assert env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True" and env["LITELLM_TELEMETRY"] == "False"
    assert env["STORE_MODEL_IN_DB"] == "True" and env["DATABASE_URL"] == "postgresql://x"
    assert "@sha256:" in spec["image"]  # pinned by digest
    assert env["DISABLE_ADMIN_UI"] == "True"
    with_ui = gateway_spec(Settings(root=tmp_path, llm_admin_ui=True), "sk-master", "sk-salt", "x")
    assert with_ui["environment"]["DISABLE_ADMIN_UI"] == "False"
    assert with_ui["labels"]["traefik.http.routers.ow-llm.rule"] == rule
    other = gateway_spec(Settings(root=tmp_path), "sk-other", "sk-salt", "postgresql://x")
    assert other["labels"]["ow.spec"] != labels["ow.spec"]


def test_session_model_changes_with_model_and_key():
    """A new session (model or key) is a new id, so it replaces the old entry."""
    first = session_model("unsloth/Qwen3.5-9B-GGUF", "ow-k1")
    assert first["model_name"] == "unsloth/Qwen3.5-9B-GGUF"
    assert first["litellm_params"] == {
        "model": "openai/unsloth/Qwen3.5-9B-GGUF",
        "api_base": "http://ow-ai-model:8000/v1",
        "api_key": "ow-k1",
    }
    assert first["model_info"]["id"].startswith("ow-session-")
    assert (
        session_model("unsloth/Qwen3.5-9B-GGUF", "ow-k2")["model_info"]["id"]
        != (first["model_info"]["id"])
    )


def test_models_follow_the_session(litellm):
    """The session's model is added, replaced and removed; other models are left alone."""
    fake, gateway = litellm
    first = session_model("a/zero", "k")
    gateway.sync_models([first])  # the gateway has no models yet: LiteLLM answers 500
    assert list(fake.models) == [first["model_info"]["id"]]
    gateway.sync_models([])
    fake.models["admin-own"] = {"model_name": "x", "model_info": {"id": "admin-own"}}
    one = session_model("a/one", "k")
    gateway.sync_models([one])
    gateway.sync_models([one])
    assert sorted(fake.models) == sorted(["admin-own", one["model_info"]["id"]])
    assert fake.calls.count("/model/new") == 2  # a/zero, then a/one only once
    two = session_model("b/two", "k")
    gateway.sync_models([two])
    assert sorted(fake.models) == sorted(["admin-own", two["model_info"]["id"]])
    gateway.sync_models([])
    assert list(fake.models) == ["admin-own"]


def test_keys_are_added_blocked_and_deleted(litellm):
    """One key per user by alias; a new key replaces the old one; block and delete by user."""
    fake, gateway = litellm
    gateway.add_key("tayla", "sk-ow-1")
    gateway.add_key("tayla", "sk-ow-2", blocked=True)
    assert fake.keys == {"sk-ow-2": {"key_alias": "ow-tayla", "blocked": True}}
    gateway.set_blocked("sk-ow-2", False)
    assert fake.keys["sk-ow-2"]["blocked"] is False
    gateway.delete_key("tayla")
    assert fake.keys == {}


def test_keys_are_synced_from_orbitalwindow(litellm):
    """Missing keys are added, the blocked state follows the user, removed users lose theirs."""
    fake, gateway = litellm
    fake.keys["sk-ow-old"] = {"key_alias": "ow-gone", "blocked": None}
    fake.keys["sk-ow-t"] = {"key_alias": "ow-tayla", "blocked": None}
    fake.keys["sk-someone"] = {"key_alias": "made-by-hand", "blocked": None}
    gateway.sync_keys(
        [
            {"name": "tayla", "disabled": True, "key": "sk-ow-t"},
            {"name": "jsmith", "disabled": False, "key": "sk-ow-j"},
        ]
    )
    assert fake.keys == {
        "sk-ow-t": {"key_alias": "ow-tayla", "blocked": True},
        "sk-someone": {"key_alias": "made-by-hand", "blocked": None},
        "sk-ow-j": {"key_alias": "ow-jsmith", "blocked": None},
    }


def test_all_keys_are_read_page_by_page(litellm):
    """More than 100 keys come in pages."""
    fake, gateway = litellm
    for number in range(205):
        fake.keys[f"sk-{number}"] = {"key_alias": f"ow-u{number}", "blocked": None}
    assert len(gateway._all_keys()) == 205


def test_gateway_down_is_a_gateway_error(tmp_path):
    """A gateway that can't be reached is a GatewayError, not a crash."""

    def refuse(request):
        """Refuse every connection.

        Args:
            request: The HTTP request.

        Raises:
            httpx2.ConnectError: Always.
        """
        raise httpx2.ConnectError("refused", request=request)

    gateway = Gateway(SimpleNamespace(), Settings(root=tmp_path))
    gateway._http = httpx2.Client(base_url="http://gw", transport=httpx2.MockTransport(refuse))
    with pytest.raises(GatewayError, match="can't be reached"):
        gateway.delete_key("tayla")


class FakeDocker:
    """Containers and networks enough for ``Gateway.ensure``."""

    def __init__(self):
        """No gateway container; the session network exists."""
        self.events, self.current = [], None
        self.ai = SimpleNamespace(
            name="ow-ai",
            containers=[],
            reload=lambda: None,
            connect=lambda name: self.events.append(f"ai:{name}"),
        )
        self.db = SimpleNamespace(connect=lambda c: self.events.append("db"))
        self.containers = SimpleNamespace(get=self._get, create=self._create)
        self.networks = SimpleNamespace(list=lambda names=None: [self.ai], get=lambda name: self.db)
        self.images = SimpleNamespace(get=lambda image: None)

    def _get(self, name):
        """Return the gateway container or raise NotFound."""
        if self.current is None:
            raise docker.errors.NotFound("none")
        return self.current

    def _create(self, image, **kwargs):
        """Record the container; started later."""
        self.events.append("create")
        self.current = SimpleNamespace(
            labels=kwargs["labels"], status="created", start=lambda: self.events.append("start")
        )
        return self.current


def test_ensure_starts_the_gateway_on_both_networks(tmp_path, monkeypatch):
    """It gets its database, joins both networks (the database one before it starts); kept as is."""
    made = []
    monkeypatch.setattr(gw.pg, "ensure_database", lambda *args: made.append(args[1:]))
    client = FakeDocker()
    gateway = Gateway(client, Settings(root=tmp_path))
    gateway.ensure()
    assert made == [("litellm", "litellm")]
    assert client.events == ["create", "db", "start", "ai:ow-traefik", "ai:orbitalwindow"]
    master = (tmp_path / "state" / "llm_gateway_master_key").read_text().strip()
    assert master.startswith("sk-") and oct((tmp_path / "state" / "llm_gateway_master_key")
                                            .stat().st_mode & 0o777) == "0o600"  # fmt: skip
    client.current.status = "running"
    client.events.clear()
    gateway.ensure()
    assert "create" not in client.events
