# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The AI session: one vLLM container serving one downloaded model on one GPU.

The container ``ow-ai-model`` runs on its own internal network ``ow-ai`` and serves vLLM's
OpenAI-compatible API on port 8000, protected by an API key. When the API is made reachable
from outside the server, Traefik routes ``/v1`` to it (vLLM checks the key). Whether it runs is
read from Docker each time (its healthcheck says when the model is loaded); the database keeps a
row per start for the history.
"""

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import docker
from docker.types import DeviceRequest

from .config import Settings
from .db import Database
from .users import ValidationError

log = logging.getLogger(__name__)

CONTAINER = "ow-ai-model"
NETWORK = "ow-ai"
PORT = 8000
CACHE_VOLUME = "ow-vllm-cache"  # compiled kernels and CUDA graphs: later starts are faster
ROUTER = "ow-ai-api"
API_PREFIX = "/v1"
MOUNT = "/models"
MIN_CONTEXT = 256
LABEL_REPO = "ow.ai.repo"
LABEL_SESSION = "ow.ai.session"
# Progress bars and blank lines in vLLM's log are noise on the page.
NOISE_RE = re.compile(r"\r|it/s\]|s/it\]|^\s*$")


@dataclass(frozen=True)
class SessionRequest:
    repo: str
    gpu_index: int
    context: int
    gpu_share: float
    public: bool  # route /v1 through Traefik


def session_spec(
    request: SessionRequest, settings: Settings, api_key: str, session_id: int
) -> dict[str, Any]:
    """Build the ``containers.run`` arguments for a session.

    Args:
        request: What to serve and with which resources.
        settings: Application settings (image, models folder, Traefik entrypoint).
        api_key: The key clients must send.
        session_id: The session's database id (a label).

    Returns:
        Keyword arguments for ``client.containers.run``.
    """
    labels = {
        "ow.managed": "true",
        "ow.role": "ai-model",
        LABEL_REPO: request.repo,
        LABEL_SESSION: str(session_id),
    }
    if request.public:
        labels |= {
            "traefik.enable": "true",
            "traefik.docker.network": NETWORK,
            f"traefik.http.routers.{ROUTER}.rule": f"PathPrefix(`{API_PREFIX}`)",
            f"traefik.http.routers.{ROUTER}.entrypoints": settings.entrypoint,
            f"traefik.http.routers.{ROUTER}.tls": "true",
            f"traefik.http.routers.{ROUTER}.service": ROUTER,
            f"traefik.http.services.{ROUTER}.loadbalancer.server.port": str(PORT),
        }
    return {
        "image": settings.vllm_image,
        "command": [
            f"{MOUNT}/{request.repo}",
            "--served-model-name",
            request.repo,
            "--host",
            "0.0.0.0",
            "--port",
            str(PORT),
            "--max-model-len",
            str(request.context),
            "--gpu-memory-utilization",
            f"{request.gpu_share:.2f}",
        ],  # fmt: skip
        "name": CONTAINER,
        "hostname": CONTAINER,
        "detach": True,
        "labels": labels,
        "environment": {
            "VLLM_API_KEY": api_key,
            "HF_HUB_OFFLINE": "1",
            "VLLM_NO_USAGE_STATS": "1",
            "DO_NOT_TRACK": "1",
            "TZ": settings.tz,
        },
        "volumes": {
            str(settings.models_dir): {"bind": MOUNT, "mode": "ro"},
            CACHE_VOLUME: {"bind": "/root/.cache", "mode": "rw"},
        },
        "device_requests": [
            DeviceRequest(device_ids=[str(request.gpu_index)], capabilities=[["gpu"]])
        ],
        "shm_size": "2g",
        "network": NETWORK,
        "security_opt": ["no-new-privileges"],
        "healthcheck": {
            "test": ["CMD", "curl", "-fsS", "-o", "/dev/null", f"http://localhost:{PORT}/health"],
            "interval": 10 * 10**9,
            "timeout": 5 * 10**9,
            # Loading, compiling and capturing CUDA graphs can take many minutes the first time.
            "start_period": 30 * 60 * 10**9,
            "start_interval": 5 * 10**9,
            "retries": 3,
        },
        "restart_policy": {"Name": "no"},
    }


@dataclass(frozen=True)
class SessionState:
    status: str  # stopped | starting | running | failed
    repo: str = ""
    gpu_index: int | None = None
    context: int | None = None
    gpu_share: float | None = None
    public: bool = False
    started_by: str = ""
    started_at: datetime | None = None
    error: str = ""


def clean_log(text: str, lines: int = 20) -> str:
    """Return the end of vLLM's log without progress bars.

    Args:
        text: Container log.
        lines: How many lines to keep.

    Returns:
        The last meaningful lines.
    """
    kept = [line for line in text.splitlines() if not NOISE_RE.search(line)]
    return "\n".join(kept[-lines:])


class AiSessions:
    def __init__(self, client: docker.DockerClient, db: Database, settings: Settings) -> None:
        """Create the session manager.

        Args:
            client: Docker client.
            db: Service database.
            settings: Application settings.
        """
        self._client = client
        self._db = db
        self._settings = settings
        self._lock = threading.Lock()
        self._launching: int | None = None  # session id while its image is pulled / started

    def _container(self) -> Any | None:
        """Return the session container, if there is one.

        Returns:
            The container, or None.
        """
        try:
            return self._client.containers.get(CONTAINER)
        except docker.errors.NotFound:
            return None

    def state(self) -> SessionState:
        """Read the session's state from Docker and its database row.

        Returns:
            The state; ``failed`` includes the end of vLLM's log.
        """
        container = self._container()
        row = self._db.one("SELECT * FROM ai_sessions ORDER BY id DESC LIMIT 1")
        if row is not None and container is None and self._launching == row["id"]:
            return self._state("starting", row)
        if container is None or row is None:
            if row is not None and row["status"] == "failed":
                return self._state("failed", row)
            if row is not None and row["status"] == "started":
                # Gone without a stop (removed by hand, or the service restarted mid-start).
                self._db.execute(
                    "UPDATE ai_sessions SET status = 'stopped', stopped_at = now() WHERE id = ?",
                    [row["id"]],
                )
            return SessionState("stopped")
        if str(row["id"]) != container.labels.get(LABEL_SESSION):
            return SessionState("starting", repo=container.labels.get(LABEL_REPO, ""))
        container.reload()
        health = container.attrs.get("State", {}).get("Health", {}).get("Status")
        if container.status == "running" and health == "healthy":
            return self._state("running", row)
        if container.status in ("created", "running") and health in (None, "starting"):
            return self._state("starting", row)
        error = clean_log(container.logs(tail=200).decode(errors="replace")) or "vLLM stopped."
        self._db.execute(
            "UPDATE ai_sessions SET status = 'failed', error = ?, stopped_at = now() "
            "WHERE id = ? AND status <> 'failed'",
            [error, row["id"]],
        )
        return self._state("failed", row | {"error": error})

    @staticmethod
    def _state(status: str, row: dict[str, Any]) -> SessionState:
        """Build a state from a session row.

        Args:
            status: The status to report.
            row: The session's database row.

        Returns:
            The state.
        """
        return SessionState(
            status=status,
            repo=row["repo"],
            gpu_index=row["gpu_index"],
            context=row["context"],
            gpu_share=row["gpu_share"],
            public=row["public"],
            started_by=row["started_by"],
            started_at=row["started_at"],
            error=row["error"] if status == "failed" else "",
        )

    def log_tail(self, lines: int = 20) -> str:
        """Return the end of the running session's log.

        Args:
            lines: How many lines.

        Returns:
            The lines, or an empty string without a session.
        """
        container = self._container()
        if container is None:
            return ""
        return clean_log(container.logs(tail=300).decode(errors="replace"), lines)

    def start(
        self, request: SessionRequest, api_key: str, actor: str, *, wait: bool = False
    ) -> int:
        """Record a new session, then pull the image and run the container in the background.

        Args:
            request: What to serve and with which resources.
            api_key: The key clients must send.
            actor: Who started it.
            wait: Run the launch in this thread (tests).

        Returns:
            The session id.

        Raises:
            ValidationError: If a session is already starting or running.
        """
        with self._lock:
            if self._launching is not None:
                raise ValidationError("An AI session is already starting.")
            container = self._container()
            if container is not None:
                container.reload()
                if container.status in ("created", "running", "restarting"):
                    raise ValidationError(
                        "An AI session is already running. Stop it before starting another model."
                    )
                container.remove(force=True)  # a failed or stopped one
            session_id = self._db.scalar(
                "INSERT INTO ai_sessions (repo, gpu_index, context, gpu_share, public, status, "
                "started_by) VALUES (?, ?, ?, ?, ?, 'started', ?) RETURNING id",
                [
                    request.repo,
                    request.gpu_index,
                    request.context,
                    request.gpu_share,
                    request.public,
                    actor,
                ],
            )
            self._launching = session_id
        if wait:
            self._launch(request, api_key, session_id)
        else:
            threading.Thread(
                target=self._launch,
                args=(request, api_key, session_id),
                name="ow-ai-start",
                daemon=True,
            ).start()
        return session_id

    def _launch(self, request: SessionRequest, api_key: str, session_id: int) -> None:
        """Pull the image if needed and run the container; record a failure on the session.

        Args:
            request: What to serve and with which resources.
            api_key: The key clients must send.
            session_id: The session's database id.
        """
        try:
            network = self._ensure_network()
            self._ensure_image()
            spec = session_spec(request, self._settings, api_key, session_id)
            image = spec.pop("image")
            self._client.containers.run(image, **spec)
            if request.public:
                self._connect(network, self._settings.traefik_container)
            log.info("AI session %s: %s on GPU %s", session_id, request.repo, request.gpu_index)
        except (docker.errors.DockerException, ValidationError) as exc:
            log.warning("AI session %s could not start: %s", session_id, exc)
            self._db.execute(
                "UPDATE ai_sessions SET status = 'failed', error = ?, stopped_at = now() "
                "WHERE id = ?",
                [f"The AI session could not start: {exc}", session_id],
            )
        finally:
            self._launching = None

    def stop(self) -> str | None:
        """Stop and remove the session container.

        Returns:
            The model that was served, or None if no session existed.
        """
        container = self._container()
        row = self._db.one("SELECT * FROM ai_sessions ORDER BY id DESC LIMIT 1")
        if row is not None:
            self._db.execute(
                "UPDATE ai_sessions SET status = 'stopped', stopped_at = now() "
                "WHERE id = ? AND status = 'started'",
                [row["id"]],
            )
        if container is None:
            return None
        repo = container.labels.get(LABEL_REPO)
        container.remove(force=True)
        return repo

    def ensure_connected(self) -> None:
        """Reconnect Traefik to the session network if the API is public (after a restart)."""
        container = self._container()
        if container is None or "traefik.enable" not in container.labels:
            return
        self._connect(self._ensure_network(), self._settings.traefik_container)

    def _ensure_network(self) -> Any:
        """Return the session network, creating it if needed (internal: no internet).

        Returns:
            The Docker network.
        """
        for net in self._client.networks.list(names=[NETWORK]):
            if net.name == NETWORK:
                return net
        return self._client.networks.create(
            NETWORK, driver="bridge", internal=True, labels={"ow.managed": "true"}
        )

    def _connect(self, network: Any, container: str) -> None:
        """Connect a container to a network, if not already connected.

        Args:
            network: Docker network.
            container: Container name.
        """
        network.reload()
        if any(member.name == container for member in network.containers):
            return
        try:
            network.connect(container)
        except docker.errors.APIError as exc:
            if "already exists" not in str(exc):
                raise

    def _ensure_image(self) -> None:
        """Pull the vLLM image unless it is already there (it is large: tens of GB).

        Raises:
            ValidationError: If it can't be pulled.
        """
        try:
            self._client.images.get(self._settings.vllm_image)
        except docker.errors.ImageNotFound:
            log.info("pulling %s", self._settings.vllm_image)
            try:
                self._client.images.pull(self._settings.vllm_image)
            except docker.errors.DockerException as exc:
                raise ValidationError(f"Could not pull {self._settings.vllm_image}: {exc}") from exc
