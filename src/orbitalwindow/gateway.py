# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The LLM gateway (ADR 0045, #79): LiteLLM in front of the models, the only API for programs.

``ow-llm-gateway`` runs all the time, on the session network ``ow-ai`` (no internet; it reaches the
model there) and the database network (its own database ``litellm`` on ``ow-db``). Traefik serves
only the OpenAI endpoints programs need at ``https://<host>:8443/v1/…``; LiteLLM's admin API and
pages are reachable only from OrbitalWindow, with the master key.

OrbitalWindow owns the keys: each user gets one LiteLLM *virtual key* with a value OrbitalWindow
makes and keeps (so the dashboard can show it again), blocked while the user is disabled and
deleted with the user. The running AI session's model is added to the gateway, and removed when
the session stops; clients keep the same URL and key whatever model runs.
"""

import hashlib
import logging
import secrets
from typing import Any

import docker
import httpx2

from . import aisession, pg
from .config import Settings

log = logging.getLogger(__name__)

CONTAINER = "ow-llm-gateway"
PORT = 4000
DB_NAME = "litellm"  # its database and role on the PostgreSQL server
ROUTER = "ow-llm"
LABEL_SPEC = "ow.spec"
# Model ids the gateway gets from OrbitalWindow; other models are left alone.
MODEL_ID_PREFIX = "ow-session-"
# The OpenAI endpoints Traefik passes to the gateway; nothing else of LiteLLM is reachable.
API_RULE = (
    "Path(`/v1/models`) || PathPrefix(`/v1/models/`) || Path(`/v1/chat/completions`) "
    "|| Path(`/v1/completions`) || Path(`/v1/embeddings`)"
)


class GatewayError(Exception):
    """The gateway can't be reached or refused a request."""


def key_alias(name: str) -> str:
    """Return the alias of a user's key in the gateway.

    Args:
        name: OrbitalWindow user name.

    Returns:
        ``ow-<name>``.
    """
    return f"ow-{name}"


def new_key() -> str:
    """Return a new user key (LiteLLM wants the ``sk-`` prefix).

    Returns:
        ``sk-ow-`` and 48 hex characters.
    """
    return "sk-ow-" + secrets.token_hex(24)


def gateway_spec(
    settings: Settings, master_key: str, salt_key: str, database_url: str
) -> dict[str, Any]:
    """Build the ``containers.create`` arguments for the gateway.

    Args:
        settings: Application settings (image, Traefik entrypoint).
        master_key: The admin key OrbitalWindow uses for keys and models.
        salt_key: Encrypts the models' API keys in the gateway's database.
        database_url: The gateway's own database on the PostgreSQL server.

    Returns:
        Keyword arguments for ``client.containers.create`` (including ``image``).
    """
    entry = settings.chat_entrypoint
    labels = {
        "ow.managed": "true",
        "ow.role": "llm-gateway",
        "traefik.enable": "true",
        "traefik.docker.network": aisession.NETWORK,
        f"traefik.http.routers.{ROUTER}.rule": API_RULE,
        # Above the chat's catch-all router on the same entrypoint.
        f"traefik.http.routers.{ROUTER}.priority": "300",
        f"traefik.http.routers.{ROUTER}.entrypoints": entry,
        f"traefik.http.routers.{ROUTER}.tls": "true",
        f"traefik.http.routers.{ROUTER}.service": ROUTER,
        f"traefik.http.services.{ROUTER}.loadbalancer.server.port": str(PORT),
    }
    environment = {
        "LITELLM_MASTER_KEY": master_key,
        "LITELLM_SALT_KEY": salt_key,
        "DATABASE_URL": database_url,
        # Models come from OrbitalWindow at runtime (/model/new), kept in the database.
        "STORE_MODEL_IN_DB": "True",
        # Offline: the bundled price list, no telemetry, no admin pages.
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "LITELLM_TELEMETRY": "False",
        "DISABLE_ADMIN_UI": "True",
        "NO_DOCS": "True",
        "TZ": settings.tz,
    }
    kwargs: dict[str, Any] = {
        "image": settings.litellm_image,
        "command": ["--port", str(PORT)],
        "name": CONTAINER,
        "hostname": CONTAINER,
        "environment": environment,
        "network": aisession.NETWORK,
        "healthcheck": {
            "test": [
                "CMD",
                "python",
                "-c",
                f"import urllib.request; urllib.request.urlopen("
                f"'http://localhost:{PORT}/health/liveliness', timeout=5)",
            ],
            "interval": 10_000_000_000,
            "timeout": 10_000_000_000,
            "retries": 6,
            "start_period": 60_000_000_000,
        },
        "security_opt": ["no-new-privileges"],
        "restart_policy": {"Name": "unless-stopped"},
    }
    digest = hashlib.sha256(
        repr((sorted((k, repr(v)) for k, v in kwargs.items()), labels)).encode()
    ).hexdigest()[:16]
    kwargs["labels"] = labels | {LABEL_SPEC: digest}
    return kwargs


def session_model(repo: str, api_key: str) -> dict[str, Any]:
    """Return how the gateway reaches the AI session's model.

    The id changes with the model and the session's key, so a new session replaces the entry.

    Args:
        repo: The model id the session serves.
        api_key: The session's API key.

    Returns:
        The ``/model/new`` request body.
    """
    digest = hashlib.sha256(f"{repo}\0{api_key}".encode()).hexdigest()[:12]
    return {
        "model_name": repo,
        "litellm_params": {
            # openai/: an OpenAI-compatible server, for chat and /v1/completions (FIM) alike.
            "model": f"openai/{repo}",
            "api_base": f"http://{aisession.CONTAINER}:{aisession.PORT}/v1",
            "api_key": api_key,
        },
        "model_info": {"id": MODEL_ID_PREFIX + digest},
    }


class Gateway:
    def __init__(self, client: docker.DockerClient, settings: Settings) -> None:
        """Create the gateway manager.

        Args:
            client: Docker client (through the socket proxy).
            settings: Application settings.
        """
        self._client = client
        self._settings = settings
        self._http = httpx2.Client(base_url=f"http://{CONTAINER}:{PORT}", timeout=15)

    def _secret(self, name: str) -> str:
        """Return a secret of the gateway kept in ``state/``, making it the first time.

        Args:
            name: File name, e.g. ``llm_gateway_master_key``.

        Returns:
            The secret.
        """
        path = self._settings.state_dir / name
        try:
            value = path.read_text().strip()
        except FileNotFoundError:
            value = ""
        if not value:
            value = "sk-" + secrets.token_hex(24)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value + "\n")
            path.chmod(0o600)
        return value

    def _container(self) -> Any:
        """Return the gateway container, or None.

        Returns:
            The container, or None when there is none.
        """
        try:
            return self._client.containers.get(CONTAINER)
        except docker.errors.NotFound:
            return None

    def ready(self) -> bool:
        """Tell whether the gateway is up (its own healthcheck passed).

        Returns:
            True when the container is running and healthy.
        """
        container = self._container()
        if container is None:
            return False
        container.reload()
        health = container.attrs.get("State", {}).get("Health", {}).get("Status")
        return container.status == "running" and health == "healthy"

    def ensure(self) -> None:
        """Run the gateway, recreating it when its settings changed."""
        pg.ensure_database(self._settings, DB_NAME, DB_NAME)
        spec = gateway_spec(
            self._settings,
            self._secret("llm_gateway_master_key"),
            self._secret("llm_gateway_salt_key"),
            pg.program_url(self._settings, DB_NAME),
        )
        network = aisession.ensure_network(self._client)
        container = self._container()
        if container is not None:
            if container.labels.get(LABEL_SPEC) == spec["labels"][LABEL_SPEC]:
                if container.status != "running":
                    container.start()
                aisession.join_network(network, self._members())
                return
            container.remove(force=True)
        image = spec.pop("image")
        try:
            self._client.images.get(image)
        except docker.errors.ImageNotFound:
            log.info("pulling %s", image)
            self._client.images.pull(image)
        log.info("starting the LLM gateway")
        # Created first, so it is on the database network before it starts.
        container = self._client.containers.create(image, **spec)
        self._client.networks.get(pg.NETWORK).connect(container)
        container.start()
        aisession.join_network(network, self._members())

    def _members(self) -> tuple[str, ...]:
        """Return the containers that must reach the gateway on the session network.

        Returns:
            Traefik (for programs) and the service (for keys and models).
        """
        return (self._settings.traefik_container, self._settings.service_container)

    # --- the gateway's admin API -------------------------------------------------------------

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Call the gateway's admin API with the master key.

        Args:
            method: HTTP method.
            path: Path, e.g. ``/key/generate``.
            body: JSON body.

        Returns:
            The decoded JSON answer, or None for a 404.

        Raises:
            GatewayError: When the gateway can't be reached or refuses.
        """
        headers = {"Authorization": f"Bearer {self._secret('llm_gateway_master_key')}"}
        try:
            response = self._http.request(method, path, json=body, headers=headers)
        except httpx2.HTTPError as exc:
            raise GatewayError(f"The LLM gateway can't be reached: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise GatewayError(
                f"The LLM gateway refused {path} ({response.status_code}): {response.text[:200]}"
            )
        return response.json()

    def sync_models(self, wanted: list[dict[str, Any]]) -> None:
        """Make the gateway's OrbitalWindow models match ``wanted``.

        Args:
            wanted: ``/model/new`` bodies (see ``session_model``); empty without a session.

        Raises:
            GatewayError: When the gateway can't be reached or refuses.
        """
        try:
            info = self._call("GET", "/model/info") or {"data": []}
        except GatewayError as exc:
            # Without any model LiteLLM answers 500 "LLM Model List not loaded".
            if "not loaded" not in str(exc):
                raise
            info = {"data": []}
        present = {
            row["model_info"]["id"]
            for row in info.get("data", [])
            if str(row.get("model_info", {}).get("id", "")).startswith(MODEL_ID_PREFIX)
        }
        wanted_ids = {model["model_info"]["id"] for model in wanted}
        for model_id in present - wanted_ids:
            log.info("LLM gateway: removing model %s", model_id)
            self._call("POST", "/model/delete", {"id": model_id})
        for model in wanted:
            if model["model_info"]["id"] not in present:
                log.info("LLM gateway: adding %s", model["model_name"])
                self._call("POST", "/model/new", model)

    def add_key(self, name: str, key: str, blocked: bool = False) -> None:
        """Give a user a key in the gateway, replacing the user's old one.

        Args:
            name: OrbitalWindow user name.
            key: The key (see ``new_key``).
            blocked: Block it at once (a disabled user).

        Raises:
            GatewayError: When the gateway can't be reached or refuses.
        """
        self._call("POST", "/key/delete", {"key_aliases": [key_alias(name)]})
        self._call(
            "POST",
            "/key/generate",
            {"key": key, "user_id": name, "key_alias": key_alias(name), "metadata": {"ow": True}},
        )
        if blocked:
            self.set_blocked(key, True)

    def set_blocked(self, key: str, blocked: bool) -> None:
        """Block or unblock a key.

        Args:
            key: The key.
            blocked: Whether requests with it are refused.

        Raises:
            GatewayError: When the gateway can't be reached or refuses.
        """
        self._call("POST", "/key/block" if blocked else "/key/unblock", {"key": key})

    def delete_key(self, name: str) -> None:
        """Delete a user's key.

        Args:
            name: OrbitalWindow user name.

        Raises:
            GatewayError: When the gateway can't be reached or refuses.
        """
        self._call("POST", "/key/delete", {"key_aliases": [key_alias(name)]})

    def _all_keys(self) -> list[dict[str, Any]]:
        """Return every key in the gateway (``/key/list`` gives at most 100 per page).

        Returns:
            The keys, as LiteLLM describes them.

        Raises:
            GatewayError: When the gateway can't be reached or refuses.
        """
        keys: list[dict[str, Any]] = []
        for page in range(1, 1000):
            listed = self._call("GET", f"/key/list?return_full_object=true&size=100&page={page}")
            rows = (listed or {}).get("keys", [])
            keys += rows
            if len(rows) < 100:
                break
        return keys

    def sync_keys(self, users: list[dict[str, Any]]) -> None:
        """Make the gateway's keys match OrbitalWindow's: one per user, blocked while disabled.

        Args:
            users: ``name``, ``disabled`` and ``key`` of every user with a key.

        Raises:
            GatewayError: When the gateway can't be reached or refuses.
        """
        aliases = {
            row.get("key_alias")
            for row in self._all_keys()
            if str(row.get("key_alias") or "").startswith("ow-")
        }
        for user in users:
            info = self._call("GET", f"/key/info?key={user['key']}")
            if info is None:
                log.info("LLM gateway: adding the key of %s", user["name"])
                self.add_key(user["name"], user["key"], bool(user["disabled"]))
            elif bool(info.get("info", {}).get("blocked")) != bool(user["disabled"]):
                self.set_blocked(user["key"], bool(user["disabled"]))
        stale = sorted(aliases - {key_alias(user["name"]) for user in users})
        if stale:
            log.info("LLM gateway: deleting keys of removed users: %s", ", ".join(stale))
            self._call("POST", "/key/delete", {"key_aliases": stale})
