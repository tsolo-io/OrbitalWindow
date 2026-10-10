# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The AI chat: Open WebUI in front of the AI session, signed in through OrbitalWindow.

``ow-openwebui`` runs while the AI session is starting or running and stops with it. It sits on
the session network ``ow-ai`` (no internet) and talks to vLLM with the session's API key. Traefik
serves it on its own port (Open WebUI can't run under a path); forwardAuth (``/auth/ai``) passes
the user's e-mail, name and role, which Open WebUI trusts to sign people in. Requests carrying an
Open WebUI API key (``Bearer sk-…``) skip forwardAuth and Open WebUI checks the key.
"""

import hashlib
import logging
import secrets
from typing import Any

import docker

from . import aisession
from .config import Settings

log = logging.getLogger(__name__)

CONTAINER = "ow-openwebui"
PORT = 8080
ROUTER = "ow-chat"
API_ROUTER = "ow-chat-api"
LABEL_SPEC = "ow.spec"
DATA_MOUNT = "/data"


def chat_spec(settings: Settings, api_key: str, secret_key: str, hostname: str) -> dict[str, Any]:
    """Build the ``containers.run`` arguments for Open WebUI.

    Args:
        settings: Application settings (image, data folder, Traefik entrypoint, port).
        api_key: The AI session's API key, for Open WebUI's connection to vLLM.
        secret_key: Open WebUI's own signing key (kept so sign-ins survive a recreate).
        hostname: Host name users reach OrbitalWindow on.

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

    def ensure(self, wanted: bool, api_key: str, hostname: str) -> None:
        """Run Open WebUI while the AI session runs, and remove it otherwise.

        A container whose settings changed (a new API key, image or host name) is recreated.

        Args:
            wanted: Whether the AI session is starting or running.
            api_key: The AI session's API key.
            hostname: Host name users reach OrbitalWindow on.
        """
        container = self._container()
        if not wanted:
            if container is not None:
                log.info("stopping the AI chat")
                container.remove(force=True)
            return
        spec = chat_spec(self._settings, api_key, self.secret_key(), hostname)
        if container is not None:
            if container.labels.get(LABEL_SPEC) == spec["labels"][LABEL_SPEC]:
                if container.status != "running":
                    container.start()
                self._connect_traefik()
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
        self._connect_traefik()

    def _connect_traefik(self) -> None:
        """Connect Traefik to the session network so it can reach Open WebUI."""
        for network in self._client.networks.list(names=[aisession.NETWORK]):
            if network.name != aisession.NETWORK:
                continue
            network.reload()
            traefik = self._settings.traefik_container
            if any(member.name == traefik for member in network.containers):
                return
            try:
                network.connect(traefik)
            except docker.errors.APIError as exc:
                if "already exists" not in str(exc):
                    raise
