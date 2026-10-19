# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The AI chat: Open WebUI in front of the AI session, signed in through OrbitalWindow.

``ow-openwebui`` runs while the AI session is starting or running and stops with it. It sits on
the session network ``ow-ai`` (no internet) and talks to vLLM with the session's API key. Traefik
serves it on its own port (Open WebUI can't run under a path); forwardAuth (``/auth/ai``) passes
the user's e-mail, name and role, which Open WebUI trusts to sign people in. Requests carrying an
Open WebUI API key (``Bearer sk-…``) skip forwardAuth and Open WebUI checks the key.

``ChatKeys`` gets those keys for the dashboard (#61): the service joins ``ow-ai`` and signs the
user in with the same headers forwardAuth sends, then reads or creates the user's key.
"""

import hashlib
import logging
import secrets
from typing import Any
from urllib.parse import quote

import docker
import httpx2

from . import aisession
from .config import Settings

log = logging.getLogger(__name__)

CONTAINER = "ow-openwebui"
PORT = 8080
ROUTER = "ow-chat"
API_ROUTER = "ow-chat-api"
LABEL_SPEC = "ow.spec"
DATA_MOUNT = "/data"
# Chat accounts are <user>@<this domain>; it is never used for mail.
CHAT_EMAIL_DOMAIN = "orbitalwindow.local"


def trusted_headers(name: str, display_name: str, is_admin: bool) -> dict[str, str]:
    """Return the headers Open WebUI trusts to sign an OrbitalWindow user in.

    Args:
        name: OrbitalWindow user name.
        display_name: The user's display name.
        is_admin: OrbitalWindow admins are Open WebUI admins; everyone else is a user.

    Returns:
        ``X-OW-Email``, ``X-OW-Name`` (URL-encoded) and ``X-OW-Role``.
    """
    return {
        "X-OW-Email": f"{name}@{CHAT_EMAIL_DOMAIN}",
        "X-OW-Name": quote(display_name),
        "X-OW-Role": "admin" if is_admin else "user",
    }


class ChatKeyError(Exception):
    """Open WebUI could not be reached or refused, so the user's chat key is not available."""


class ChatKeys:
    """Reads and creates users' Open WebUI API keys (the ``sk-`` keys for ``:8443/api``)."""

    def __init__(
        self, base_url: str = f"http://{CONTAINER}:{PORT}", client: httpx2.Client | None = None
    ) -> None:
        """Create the client.

        Args:
            base_url: Open WebUI inside the chat network.
            client: HTTP client to use (tests pass one with a mock transport).
        """
        self._client = client or httpx2.Client(base_url=base_url, timeout=10.0)

    def _token(self, name: str, display_name: str, is_admin: bool) -> str:
        """Sign the user in to Open WebUI, as forwardAuth does for the browser.

        Args:
            name: OrbitalWindow user name.
            display_name: The user's display name.
            is_admin: Whether the user is an OrbitalWindow admin.

        Returns:
            Open WebUI's session token for the user.

        Raises:
            ChatKeyError: When Open WebUI can't be reached or refuses.
        """
        headers = trusted_headers(name, display_name, is_admin)
        try:
            response = self._client.post(
                "/api/v1/auths/signin", headers=headers, json={"email": "", "password": ""}
            )
            response.raise_for_status()
            return response.json()["token"]
        except (httpx2.HTTPError, ValueError, KeyError) as exc:
            raise ChatKeyError(f"The chat did not answer: {exc}") from exc

    def get(self, name: str, display_name: str, is_admin: bool, create: bool = False) -> str | None:
        """Return the user's chat API key, creating one when asked.

        Args:
            name: OrbitalWindow user name.
            display_name: The user's display name.
            is_admin: Whether the user is an OrbitalWindow admin.
            create: Create a key when the user has none.

        Returns:
            The key, or None when the user has none and ``create`` is False.

        Raises:
            ChatKeyError: When Open WebUI can't be reached or refuses.
        """
        auth = {"Authorization": f"Bearer {self._token(name, display_name, is_admin)}"}
        try:
            response = self._client.get("/api/v1/auths/api_key", headers=auth)
            if response.status_code == 200 and response.json().get("api_key"):
                return response.json()["api_key"]
            if response.status_code not in (200, 404):
                response.raise_for_status()
            return self._create(auth) if create else None
        except (httpx2.HTTPError, ValueError) as exc:
            raise ChatKeyError(f"The chat did not answer: {exc}") from exc

    def renew(self, name: str, display_name: str, is_admin: bool) -> str:
        """Replace the user's chat API key; programs with the old key stop working.

        Args:
            name: OrbitalWindow user name.
            display_name: The user's display name.
            is_admin: Whether the user is an OrbitalWindow admin.

        Returns:
            The new key.

        Raises:
            ChatKeyError: When Open WebUI can't be reached or refuses.
        """
        auth = {"Authorization": f"Bearer {self._token(name, display_name, is_admin)}"}
        try:
            return self._create(auth)
        except (httpx2.HTTPError, ValueError) as exc:
            raise ChatKeyError(f"The chat did not answer: {exc}") from exc

    def _create(self, auth: dict[str, str]) -> str:
        """Create (or replace) the signed-in user's key.

        Args:
            auth: The user's Authorization header.

        Returns:
            The new key.

        Raises:
            httpx2.HTTPError: When Open WebUI refuses.
            ValueError: When its answer has no key.
        """
        response = self._client.post("/api/v1/auths/api_key", headers=auth)
        response.raise_for_status()
        key = response.json().get("api_key")
        if not key:
            raise ValueError("no key in the answer")
        return key


def chat_spec(
    settings: Settings, api_key: str, secret_key: str, hostname: str, tools: bool = True
) -> dict[str, Any]:
    """Build the ``containers.run`` arguments for Open WebUI.

    Args:
        settings: Application settings (image, data folder, Traefik entrypoint, port).
        api_key: The AI session's API key, for Open WebUI's connection to vLLM.
        secret_key: Open WebUI's own signing key (kept so sign-ins survive a recreate).
        hostname: Host name users reach OrbitalWindow on.
        tools: Whether vLLM can call tools for the model; if not, Open WebUI describes tools in
            the prompt instead of sending them (vLLM refuses those requests).

    Returns:
        Keyword arguments for ``client.containers.run`` (including ``image``).
    """
    entry = settings.chat_entrypoint
    labels = {
        "ow.managed": "true",
        "ow.role": "chat",
        "traefik.enable": "true",
        "traefik.docker.network": aisession.NETWORK,
        f"traefik.http.routers.{ROUTER}.rule": "PathPrefix(`/`)",
        f"traefik.http.routers.{ROUTER}.entrypoints": entry,
        f"traefik.http.routers.{ROUTER}.tls": "true",
        f"traefik.http.routers.{ROUTER}.service": ROUTER,
        f"traefik.http.routers.{ROUTER}.middlewares": "ow-auth-ai@docker",
        # Programs with an Open WebUI API key: no OrbitalWindow session, Open WebUI checks the key.
        f"traefik.http.routers.{API_ROUTER}.rule": (
            "PathPrefix(`/api`) && HeaderRegexp(`Authorization`, `^Bearer sk-`)"
        ),
        f"traefik.http.routers.{API_ROUTER}.priority": "100",
        f"traefik.http.routers.{API_ROUTER}.entrypoints": entry,
        f"traefik.http.routers.{API_ROUTER}.tls": "true",
        f"traefik.http.routers.{API_ROUTER}.service": ROUTER,
        f"traefik.http.routers.{API_ROUTER}.middlewares": "ow-chat-strip@docker",
        f"traefik.http.services.{ROUTER}.loadbalancer.server.port": str(PORT),
    }
    environment = {
        "WEBUI_URL": f"https://{hostname}:{settings.chat_port}",
        # Our data folder, not /app/backend/data: the image keeps its embedding and speech models
        # under /app/backend/data/cache, and without internet they can't be fetched again.
        "DATA_DIR": DATA_MOUNT,
        "WEBUI_SECRET_KEY": secret_key,
        "CORS_ALLOW_ORIGIN": f"https://{hostname}:{settings.chat_port}",
        "WEBUI_AUTH_TRUSTED_EMAIL_HEADER": "X-OW-Email",
        "WEBUI_AUTH_TRUSTED_NAME_HEADER": "X-OW-Name",
        "WEBUI_AUTH_TRUSTED_ROLE_HEADER": "X-OW-Role",
        "ENABLE_LOGIN_FORM": "false",
        "ENABLE_SIGNUP": "false",
        "DEFAULT_USER_ROLE": "user",
        "ENABLE_API_KEYS": "true",
        "USER_PERMISSIONS_FEATURES_API_KEYS": "true",  # every user, not only admins
        "ENABLE_OLLAMA_API": "false",
        "OPENAI_API_BASE_URL": f"http://{aisession.CONTAINER}:{aisession.PORT}/v1",
        "OPENAI_API_KEY": api_key,
        # Everyone may use the session's model; there is only one.
        "BYPASS_MODEL_ACCESS_CONTROL": "true",
        "ENABLE_EVALUATION_ARENA_MODELS": "false",  # no extra "arena-model" in the model list
        "ENABLE_COMMUNITY_SHARING": "false",
        "OFFLINE_MODE": "true",
        "HF_HUB_OFFLINE": "1",
        "ANONYMIZED_TELEMETRY": "false",
        "DO_NOT_TRACK": "true",
        # The environment is the configuration: no settings that differ from it after a restart.
        "ENABLE_PERSISTENT_CONFIG": "false",
        "TZ": settings.tz,
    }
    if not tools:
        environment["DEFAULT_MODEL_PARAMS"] = '{"function_calling": "legacy"}'
    kwargs: dict[str, Any] = {
        "image": settings.openwebui_image,
        "name": CONTAINER,
        "hostname": CONTAINER,
        "detach": True,
        "environment": environment,
        "volumes": {str(settings.chat_data_dir): {"bind": DATA_MOUNT, "mode": "rw"}},
        "network": aisession.NETWORK,
        "security_opt": ["no-new-privileges"],
        "restart_policy": {"Name": "unless-stopped"},
    }
    digest = hashlib.sha256(
        repr((sorted((k, repr(v)) for k, v in kwargs.items()), labels)).encode()
    ).hexdigest()[:16]
    kwargs["labels"] = labels | {LABEL_SPEC: digest}
    return kwargs


class Chat:
    def __init__(self, client: docker.DockerClient, settings: Settings) -> None:
        """Create the chat manager.

        Args:
            client: Docker client.
            settings: Application settings.
        """
        self._client = client
        self._settings = settings

    def _container(self) -> Any | None:
        """Return the Open WebUI container, if there is one.

        Returns:
            The container, or None.
        """
        try:
            return self._client.containers.get(CONTAINER)
        except docker.errors.NotFound:
            return None

    def secret_key(self) -> str:
        """Return Open WebUI's signing key, creating it on first use.

        Returns:
            The key.
        """
        path = self._settings.chat_data_dir.parent / "openwebui_secret"
        try:
            key = path.read_text().strip()
        except FileNotFoundError:
            key = ""
        if not key:
            key = secrets.token_urlsafe(32)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(key + "\n")
            path.chmod(0o600)
        return key

    def ready(self) -> bool:
        """Tell whether Open WebUI is up (its own healthcheck passed).

        Returns:
            True when the container is running and healthy.
        """
        container = self._container()
        if container is None:
            return False
        container.reload()
        health = container.attrs.get("State", {}).get("Health", {}).get("Status")
        return container.status == "running" and health in ("healthy", None)

    def ensure(self, wanted: bool, api_key: str, hostname: str, tools: bool = True) -> None:
        """Run Open WebUI while the AI session runs, and remove it otherwise.

        A container whose settings changed (a new API key, image, host name or tool calling) is
        recreated.

        Args:
            wanted: Whether the AI session is starting or running.
            api_key: The AI session's API key.
            hostname: Host name users reach OrbitalWindow on.
            tools: Whether vLLM can call tools for the session's model.
        """
        container = self._container()
        if not wanted:
            if container is not None:
                log.info("stopping the AI chat")
                container.remove(force=True)
            return
        spec = chat_spec(self._settings, api_key, self.secret_key(), hostname, tools)
        if container is not None:
            if container.labels.get(LABEL_SPEC) == spec["labels"][LABEL_SPEC]:
                if container.status != "running":
                    container.start()
                self._connect_members()
                return
            container.remove(force=True)
        self._settings.chat_data_dir.mkdir(parents=True, exist_ok=True)
        image = spec.pop("image")
        try:
            self._client.images.get(image)
        except docker.errors.ImageNotFound:
            log.info("pulling %s", image)
            self._client.images.pull(image)
        log.info("starting the AI chat")
        self._client.containers.run(image, **spec)
        self._connect_members()

    def _connect_members(self) -> None:
        """Connect Traefik (for the chat) and the service (for chat keys) to the session network."""
        wanted = (self._settings.traefik_container, self._settings.service_container)
        for network in self._client.networks.list(names=[aisession.NETWORK]):
            if network.name != aisession.NETWORK:
                continue
            network.reload()
            present = {member.name for member in network.containers}
            for name in wanted:
                if name in present:
                    continue
                try:
                    network.connect(name)
                except docker.errors.NotFound:
                    log.info("%s is not a container here; not joining the chat network", name)
                except docker.errors.APIError as exc:
                    if "already exists" not in str(exc):
                        raise
