# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""VS Code in the browser without a desktop: code-server in ``ow-code-<user>`` (ADR 0040).

It is LinuxServer's code-server image with OrbitalWindow's changes
(``deploy/image/Dockerfile.code``), built on first use as ``ow-code:<hash>``. It runs as the user's
UID with their home at ``/home`` (``/config`` in the desktop) and the desktop's other folders,
starts and stops on demand, and has no password: like the file manager it lives on its own
network ``ow-cnet-<user>`` that only Traefik joins, and Traefik's forwardAuth lets only the owner
in.
"""

import contextlib
import hashlib
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import docker

from .config import Settings
from .desktops import LABEL_GROUPS, LABEL_MANAGED, LABEL_ROLE, LABEL_USER
from .userenv import LABEL_ENV, NAMES_VAR, RESERVED, env_hash

log = logging.getLogger(__name__)

CODE_PORT = 8443
CODE_PIDS_LIMIT = 2048
# The files the image is built from (relative to the image context); their hash is its tag.
IMAGE_FILES = (
    "Dockerfile.code",
    "code",
    "root/custom-cont-init.d/50-ow-setup",
    "root/usr/local/bin/ow-pkg",
)


class CodeState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"  # image being built or pulled, or code-server not answering yet
    RUNNING = "running"
    ERROR = "error"


def code_container_name(user: str) -> str:
    """Return the Docker container name of a user's VS Code.

    Args:
        user: OrbitalWindow user name.

    Returns:
        ``ow-code-<user>``.
    """
    return f"ow-code-{user}"


def code_network_name(user: str) -> str:
    """Return the name of the network of a user's VS Code.

    Args:
        user: OrbitalWindow user name.

    Returns:
        ``ow-cnet-<user>``.
    """
    return f"ow-cnet-{user}"


def code_path(user: str) -> str:
    """Return the URL path of a user's VS Code.

    Args:
        user: OrbitalWindow user name.

    Returns:
        ``/code/<user>/``.
    """
    return f"/code/{user}/"


def image_tag(context: Path, base_image: str) -> str:
    """Return the tag of the VS Code image: a hash of its build files and base image.

    Args:
        context: The image build context (``deploy/image``).
        base_image: The code-server image it is built on.

    Returns:
        ``ow-code:<12 hex digits>``.
    """
    digest = hashlib.sha256(base_image.encode())
    for name in IMAGE_FILES:
        path = context / name
        files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
        for file in files:
            digest.update(str(file.relative_to(context)).encode())
            digest.update(file.read_bytes())
    return f"ow-code:{digest.hexdigest()[:12]}"


@dataclass(frozen=True)
class CodeSpec:
    name: str
    network: str
    kwargs: dict[str, Any]


def build_code_spec(
    user: dict[str, Any],
    groups: list[dict[str, Any]],
    settings: Settings,
    user_env: dict[str, str] | None = None,
) -> CodeSpec:
    """Build the container spec of a user's VS Code.

    Args:
        user: User row from the database (its CPU and RAM limits apply).
        groups: Group rows whose folders are mounted.
        settings: Service settings.
        user_env: The user's own environment variables.

    Returns:
        The container spec.
    """
    name = user["name"]
    cname = code_container_name(name)
    net = code_network_name(name)
    root = settings.root
    uid = user["uid_gid"]
    path = code_path(name)
    router = cname
    volumes = {
        # /home, not /config as in the desktop: the folder VS Code users expect (and look in).
        str(root / "users" / name): {"bind": "/home", "mode": "rw"},
        str(root / "shared" / "scratch"): {"bind": "/scratch", "mode": "rw"},
        str(root / "shared" / "data"): {"bind": "/shared", "mode": "ro"},
    }
    for group in groups:
        volumes[str(root / "groups" / group["name"])] = {
            "bind": f"/groups/{group['name']}",
            "mode": "rw",
        }
    environment = {
        "PUID": str(uid),
        "PGID": str(uid),
        "TZ": settings.tz,
        "HOME": "/home",
        "OW_GUI": "false",  # the init hook makes no desktop icons
        "OW_GROUPS": " ".join(f"{g['name']}:{g['gid']}" for g in groups),
    }
    own = {k: v for k, v in (user_env or {}).items() if k.upper() not in RESERVED}
    environment.update(own)
    environment[NAMES_VAR] = " ".join(sorted(own))
    labels = {
        LABEL_MANAGED: "true",
        LABEL_USER: name,
        LABEL_ROLE: "code",
        LABEL_GROUPS: ",".join(g["name"] for g in groups),
        LABEL_ENV: env_hash(own),
        "traefik.enable": "true",
        "traefik.docker.network": net,
        f"traefik.http.routers.{router}.rule": f"Path(`{path[:-1]}`) || PathPrefix(`{path}`)",
        f"traefik.http.routers.{router}.entrypoints": settings.entrypoint,
        f"traefik.http.routers.{router}.tls": "true",
        f"traefik.http.routers.{router}.service": router,
        # code-server uses relative links, so it runs at "/" behind the stripped prefix.
        f"traefik.http.middlewares.{router}-strip.stripprefix.prefixes": path[:-1],
        f"traefik.http.routers.{router}.middlewares": (
            f"ow-code-slash@docker,ow-auth-code@docker,{router}-strip@docker"
        ),
        f"traefik.http.services.{router}.loadbalancer.server.port": str(CODE_PORT),
    }
    kwargs: dict[str, Any] = {
        "name": cname,
        "hostname": cname,
        "labels": labels,
        "environment": environment,
        "volumes": volumes,
        "cap_drop": ["NET_RAW"],
        "security_opt": ["no-new-privileges"],
        "pids_limit": CODE_PIDS_LIMIT,
        "healthcheck": {
            "test": [
                "CMD",
                "curl",
                "-fsS",
                "-o",
                "/dev/null",
                f"http://localhost:{CODE_PORT}/healthz",
            ],
            "interval": 30 * 10**9,
            "timeout": 3 * 10**9,
            "start_period": 60 * 10**9,
            "start_interval": 2 * 10**9,
            "retries": 3,
        },
        "restart_policy": {"Name": "unless-stopped"},
    }
    if user.get("cpus"):
        kwargs["nano_cpus"] = int(user["cpus"] * 1e9)
    if user.get("mem_gb"):
        kwargs["mem_limit"] = int(user["mem_gb"] * 1024**3)
    return CodeSpec(cname, net, kwargs)


class CodeServers:
    def __init__(self, client: docker.DockerClient, settings: Settings) -> None:
        """Create the VS Code controller.

        Args:
            client: Docker client.
            settings: Service settings.
        """
        self._client = client
        self._settings = settings
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._starting: set[str] = set()  # users whose image is built or container created
        self._errors: dict[str, str] = {}  # the last start failure per user

    def state(self, name: str) -> tuple[CodeState, str]:
        """Read a user's VS Code state from Docker.

        Args:
            name: OrbitalWindow user name.

        Returns:
            The state and, for ``error``, why.
        """
        if name in self._starting:
            return CodeState.STARTING, ""
        try:
            container = self._client.containers.get(code_container_name(name))
        except docker.errors.NotFound:
            error = self._errors.get(name, "")
            return (CodeState.ERROR, error) if error else (CodeState.STOPPED, "")
        status = container.attrs.get("State", {})
        if status.get("Status") == "running":
            health = (status.get("Health") or {}).get("Status")
            return (CodeState.STARTING if health == "starting" else CodeState.RUNNING), ""
        if status.get("Status") in ("exited", "dead"):
            logs = container.logs(tail=10).decode(errors="replace").strip()
            return CodeState.ERROR, logs or f"exit code {status.get('ExitCode')}"
        return CodeState.STARTING, ""

    def start(
        self,
        user: dict[str, Any],
        groups: list[dict[str, Any]],
        user_env: dict[str, str] | None = None,
        wait: bool = False,
    ) -> None:
        """Start a user's VS Code with the current spec; the image is built first if needed.

        Runs in the background (the first build takes a minute or two) unless ``wait``.

        Args:
            user: User row from the database.
            groups: Group rows whose folders are mounted.
            user_env: The user's own environment variables.
            wait: Run in this thread (tests).
        """
        name = user["name"]
        self._starting.add(name)
        self._errors.pop(name, None)
        if wait:
            self._launch(user, groups, user_env)
        else:
            threading.Thread(
                target=self._launch, args=(user, groups, user_env), name=f"ow-code-{name}",
                daemon=True,
            ).start()  # fmt: skip

    def _launch(
        self, user: dict[str, Any], groups: list[dict[str, Any]], user_env: dict[str, str] | None
    ) -> None:
        """Build the image if needed and (re)create the container; remember a failure.

        Args:
            user: User row from the database.
            groups: Group rows whose folders are mounted.
            user_env: The user's own environment variables.
        """
        name = user["name"]
        spec = build_code_spec(user, groups, self._settings, user_env)
        try:
            with self._locks[name]:
                image = self._ensure_image()
                with contextlib.suppress(docker.errors.NotFound):
                    self._client.containers.get(spec.name).remove(force=True)
                network = self._ensure_network(name)
                self._connect_traefik(network)
                container = self._client.containers.create(
                    image, network=spec.network, **spec.kwargs
                )
                container.start()
            log.info("VS Code for %s started (%s)", name, image)
        except docker.errors.DockerException as exc:
            log.warning("VS Code for %s could not start: %s", name, exc)
            self._errors[name] = f"VS Code could not start: {exc}"
        finally:
            self._starting.discard(name)

    def stop(self, name: str) -> None:
        """Stop and remove a user's VS Code container (its settings stay in the home).

        Args:
            name: OrbitalWindow user name.
        """
        self._errors.pop(name, None)
        with self._locks[name], contextlib.suppress(docker.errors.NotFound):
            self._client.containers.get(code_container_name(name)).remove(force=True)

    def remove(self, name: str) -> None:
        """Remove a user's VS Code container and its network.

        Args:
            name: OrbitalWindow user name.
        """
        self.stop(name)
        with self._locks[name]:
            for net in self._client.networks.list(names=[code_network_name(name)]):
                if net.name != code_network_name(name):
                    continue
                net.reload()
                for container in net.containers:
                    with contextlib.suppress(docker.errors.APIError):
                        net.disconnect(container, force=True)
                net.remove()

    def _ensure_image(self) -> str:
        """Build the VS Code image unless it exists.

        Returns:
            The image tag.

        Raises:
            docker.errors.BuildError: If the build fails.
        """
        context = self._settings.image_context
        base = self._settings.code_server_image
        tag = image_tag(context, base)
        try:
            self._client.images.get(tag)
        except docker.errors.ImageNotFound:
            log.info("building %s from %s", tag, base)
            self._client.images.build(
                path=str(context),
                dockerfile="Dockerfile.code",
                tag=tag,
                buildargs={"BASE_IMAGE": base},
                pull=True,
                rm=True,
                forcerm=True,
                labels={LABEL_MANAGED: "true", LABEL_ROLE: "code-image"},
            )
        return tag

    def _ensure_network(self, name: str) -> Any:
        """Return the network of a user's VS Code, creating it if needed.

        Args:
            name: OrbitalWindow user name.

        Returns:
            The Docker network.
        """
        net_name = code_network_name(name)
        for net in self._client.networks.list(names=[net_name]):
            if net.name == net_name:
                return net
        return self._client.networks.create(
            net_name, driver="bridge", labels={LABEL_MANAGED: "true", LABEL_USER: name}
        )

    def _connect_traefik(self, network: Any) -> None:
        """Connect the Traefik container to a network, if not already connected.

        Args:
            network: Docker network.
        """
        network.reload()
        traefik = self._settings.traefik_container
        if any(c.name == traefik for c in network.containers):
            return
        try:
            network.connect(traefik)
        except docker.errors.APIError as exc:
            if "already exists" not in str(exc):
                raise
