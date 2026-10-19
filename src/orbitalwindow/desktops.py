# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Desktop containers: the spec we create, and start / stop / status.

DuckDB is the source of truth. **Start** always creates a fresh container on the user's own
network; **Stop** removes the container and the network. The home folder stays.
"""

import base64
import contextlib
import logging
import secrets
import threading
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import docker
from docker.types import DeviceRequest, EndpointConfig

from .addons import Addon
from .config import Settings
from .db import Database
from .terminal import ShellTarget, stop_session
from .userenv import LABEL_ENV, NAMES_VAR, RESERVED, env_hash

log = logging.getLogger(__name__)

LABEL_MANAGED = "ow.managed"
LABEL_USER = "ow.user"
LABEL_ROLE = "ow.role"
LABEL_ADDONS = "ow.addons"
LABEL_GROUPS = "ow.groups"
LABEL_SESSION = "ow.session"  # start id; part of the desktop's URL path
DESKTOP_PORT = 3000
DESKTOP_PIDS_LIMIT = 4096


class GpuVendor(StrEnum):
    NVIDIA = "nvidia"
    AMD = "amd"
    INTEL = "intel"
    NONE = "none"


class DesktopState(StrEnum):
    STOPPED = "stopped"
    BUILDING = "building"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"


class DesktopError(RuntimeError):
    """A user-facing reason why a desktop action could not be done."""


def container_name(user: str) -> str:
    """Return the Docker container name of a user's desktop.

    Args:
        user: OrbitalWindow user name.

    Returns:
        ``ow-<user>``.
    """
    return f"ow-{user}"


def network_name(user: str) -> str:
    """Return the name of a user's own Docker network.

    Args:
        user: OrbitalWindow user name.

    Returns:
        ``ow-net-<user>``.
    """
    return f"ow-net-{user}"


def desktop_path(user: str, session: str = "") -> str:
    """Return the URL path of a user's desktop.

    ``/desktop/<user>/`` is stable; OrbitalWindow redirects it to the running desktop. With a
    start id, ``/desktop/<user>/<session>/`` is where one particular container is served.

    Each start gets a new path, so nothing the browser keeps per path (the stream client's
    settings in localStorage, cached files) carries over to the next container.

    Args:
        user: OrbitalWindow user name.
        session: Start id of a running desktop; empty for the stable path.

    Returns:
        ``/desktop/<user>/`` or ``/desktop/<user>/<session>/``.
    """
    return f"/desktop/{user}/{session}/" if session else f"/desktop/{user}/"


def desktop_shell(user: str) -> ShellTarget:
    """Return where a user's terminal runs: in the desktop, as ``abc``, in the home folder.

    Args:
        user: OrbitalWindow user name.

    Returns:
        The shell target of the user's desktop.
    """
    return ShellTarget(container_name(user), "abc", "/config")


def new_session_id() -> str:
    """Return a new random start id (8 hex characters) for a desktop's URL path."""
    return secrets.token_hex(4)


@dataclass(frozen=True)
class SshAccess:
    """SSH via the gateway: sshpiper labels and the upstream public key file to mount."""

    labels: dict[str, str]
    upstream_pub: Path


@dataclass(frozen=True)
class DesktopSpec:
    """Everything needed to create a desktop container (pure data, so it can be tested)."""

    name: str
    image: str
    network: str
    mac: str
    kwargs: dict[str, Any]


def build_spec(
    user: dict[str, Any],
    image_ref: str,
    settings: Settings,
    gpu_vendor: GpuVendor,
    secret: str,
    addons: Sequence[Addon] = (),
    groups: Sequence[dict[str, Any]] = (),
    ssh: SshAccess | None = None,
    session: str = "",
    user_env: Mapping[str, str] | None = None,
    gui: bool = True,
) -> DesktopSpec:
    """Build the container spec for ``user``.

    Add-on mounts and environment are applied here (no rebuild).

    Args:
        user: User row from the database.
        image_ref: Docker image to run.
        settings: Service settings.
        gpu_vendor: GPU vendor whose devices are passed through.
        secret: Password for the desktop; also sent by Traefik as HTTP basic auth.
        addons: Add-ons enabled for this start (mounts and environment).
        groups: Group rows whose folders are mounted under ``/groups``.
        ssh: SSH gateway access, or None for no SSH.
        session: Start id for the URL path; a new one is generated when empty.
        user_env: The user's own environment variables; they win over add-ons' ones.
        gui: Whether the image has a desktop. A terminal-only container gets no web route and
            none of the desktop's settings.

    Returns:
        The desktop container spec.
    """
    name = user["name"]
    cname = container_name(name)
    net = network_name(name)
    router = cname
    session = session or new_session_id()
    path = desktop_path(name, session)
    basic = base64.b64encode(f"{name}:{secret}".encode()).decode()

    labels = {
        LABEL_MANAGED: "true",
        LABEL_USER: name,
        LABEL_ROLE: "desktop",
        LABEL_SESSION: session,
        # Add-ons active at start; used to find desktops that use a revoked add-on's mounts.
        LABEL_ADDONS: ",".join(a.key for a in addons),
    }
    web_labels = {
        "traefik.enable": "true",
        "traefik.docker.network": net,
        f"traefik.http.routers.{router}.rule": f"Path(`{path[:-1]}`) || PathPrefix(`{path}`)",
        f"traefik.http.routers.{router}.entrypoints": settings.entrypoint,
        f"traefik.http.routers.{router}.tls": "true",
        f"traefik.http.routers.{router}.service": router,
        f"traefik.http.routers.{router}.middlewares": ",".join(
            [settings.slash_middleware, settings.auth_middleware, f"{router}-basic"]
        ),
        f"traefik.http.middlewares.{router}-basic.headers.customrequestheaders.Authorization": (
            f"Basic {basic}"
        ),
        f"traefik.http.services.{router}.loadbalancer.server.port": str(DESKTOP_PORT),
    }

    uid = str(user["uid_gid"])
    environment = {"PUID": uid, "PGID": uid, "TZ": settings.tz}
    if gui:
        labels.update(web_labels)
        environment.update(
            {
                "TITLE": f"{user['display_name']} – OrbitalWindow",
                "CUSTOM_USER": name,
                "PASSWORD": secret,
                "SUBFOLDER": path,
                "PIXELFLUX_WAYLAND": "false",
                "START_DOCKER": "false",
            }
        )
    else:
        environment["OW_GUI"] = "false"

    root = settings.root
    volumes = {
        str(root / "users" / name): {"bind": "/config", "mode": "rw"},
        str(root / "shared" / "scratch"): {"bind": "/scratch", "mode": "rw"},
        str(root / "shared" / "data"): {"bind": "/shared", "mode": "ro"},
    }
    for group in groups:
        volumes[str(root / "groups" / group["name"])] = {
            "bind": f"/groups/{group['name']}",
            "mode": "rw",
        }
    if ssh is not None:
        # The only key the desktop's sshd accepts; the gateway checks the user's own keys.
        volumes[str(ssh.upstream_pub)] = {"bind": "/etc/ow/upstream.pub", "mode": "ro"}
        labels.update(ssh.labels)
    # The init hook creates these groups and adds abc to them (s6 keeps supplementary groups).
    environment["OW_GROUPS"] = " ".join(f"{g['name']}:{g['gid']}" for g in groups)
    labels[LABEL_GROUPS] = ",".join(g["name"] for g in groups)
    for addon in addons:
        for mount in addon.spec.mounts:
            mode = "ro" if mount.read_only else "rw"
            volumes[str(root / mount.source)] = {"bind": mount.target, "mode": mode}
        for key, value in addon.spec.env.items():
            environment.setdefault(key, value)
    # Validated when saved; reserved names are skipped again in case the rules changed since.
    own = {k: v for k, v in (user_env or {}).items() if k.upper() not in RESERVED}
    environment.update(own)
    # The init hook writes these into a profile script, so SSH logins get them too.
    environment[NAMES_VAR] = " ".join(sorted(own))
    labels[LABEL_ENV] = env_hash(own)

    devices: list[str] = []
    device_requests: list[DeviceRequest] = []
    if gpu_vendor is GpuVendor.NVIDIA:
        devices.append("/dev/dri:/dev/dri:rwm")
        device_requests.append(DeviceRequest(count=-1, capabilities=[["gpu"]]))
        environment["NVIDIA_DRIVER_CAPABILITIES"] = "all"
    elif gpu_vendor is GpuVendor.AMD:
        devices += ["/dev/dri:/dev/dri:rwm", "/dev/kfd:/dev/kfd:rwm"]
    elif gpu_vendor is GpuVendor.INTEL:
        # Mesa (iris, ANV) and VA-API (iHD) in the desktop image need only the render nodes.
        devices.append("/dev/dri:/dev/dri:rwm")

    kwargs: dict[str, Any] = {
        "name": cname,
        "hostname": cname,
        "labels": labels,
        "environment": environment,
        "volumes": volumes,
        "devices": devices,
        "device_requests": device_requests,
        "cap_drop": ["NET_RAW"],
        # Stops a fork bomb in one desktop from exhausting the host's process table.
        "pids_limit": DESKTOP_PIDS_LIMIT,
        "shm_size": "2g",
        "restart_policy": {"Name": "unless-stopped"},
    }
    if user.get("cpus"):
        kwargs["nano_cpus"] = int(user["cpus"] * 1e9)
    if user.get("mem_gb"):
        kwargs["mem_limit"] = int(user["mem_gb"] * 1024**3)
    return DesktopSpec(cname, image_ref, net, user["mac"], kwargs)


def _state_of(state: dict[str, Any]) -> DesktopState:
    """Map Docker's ``State`` to ours. Running counts only once the healthcheck has passed.

    Args:
        state: The ``State`` section of a container's Docker attributes.

    Returns:
        The desktop state.
    """
    status = state.get("Status")
    if status == "running":
        health = (state.get("Health") or {}).get("Status")
        return DesktopState.STARTING if health == "starting" else DesktopState.RUNNING
    if status in ("exited", "dead"):
        return DesktopState.ERROR
    return DesktopState.STARTING  # created, restarting, paused


class Desktops:
    def __init__(self, client: docker.DockerClient, db: Database, settings: Settings) -> None:
        """Create the desktop manager.

        Args:
            client: Docker client.
            db: Service database.
            settings: Service settings.
        """
        self._client = client
        self._db = db
        self._settings = settings
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    # --- queries -----------------------------------------------------------------------------

    def gpu_vendor(self) -> GpuVendor:
        """Return the configured GPU vendor (NVIDIA unless set otherwise)."""
        return GpuVendor(self._db.get_setting("gpu_vendor", GpuVendor.NVIDIA))

    def state(self, user: str) -> DesktopState:
        """Return the state of a user's desktop.

        Args:
            user: OrbitalWindow user name.

        Returns:
            The desktop state; STOPPED when there is no container.
        """
        try:
            container = self._client.containers.get(container_name(user))
        except docker.errors.NotFound:
            return DesktopState.STOPPED
        return _state_of(container.attrs.get("State", {}))

    def states(self) -> dict[str, DesktopState]:
        """Return the state of every managed desktop, in one Docker call.

        Used by admin pages and reconcile.

        Returns:
            Desktop state by user name, for every user with a desktop container.
        """
        result: dict[str, DesktopState] = {}
        for container in self._client.containers.list(
            all=True, filters={"label": [f"{LABEL_MANAGED}=true", f"{LABEL_ROLE}=desktop"]}
        ):
            user = container.labels.get(LABEL_USER)
            if user:
                result[user] = _state_of(container.attrs.get("State", {}))
        return result

    # --- lifecycle ---------------------------------------------------------------------------

    def addons_in_use(self, user: str) -> set[str]:
        """Return the keys of the add-ons the running desktop was started with.

        Args:
            user: OrbitalWindow user name.

        Returns:
            Add-on keys; empty when there is no container.
        """
        try:
            container = self._client.containers.get(container_name(user))
        except docker.errors.NotFound:
            return set()
        return {k for k in container.labels.get(LABEL_ADDONS, "").split(",") if k}

    def current_path(self, user: str) -> str | None:
        """Return the URL path of the user's running desktop.

        Args:
            user: OrbitalWindow user name.

        Returns:
            ``/desktop/<user>/<session>/``, or None when there is no container or it has no
            start id.
        """
        try:
            container = self._client.containers.get(container_name(user))
        except docker.errors.NotFound:
            return None
        session = container.labels.get(LABEL_SESSION)
        return desktop_path(user, session) if session else None

    def groups_in_use(self, user: str) -> set[str] | None:
        """Return the groups the running desktop was started with.

        Args:
            user: OrbitalWindow user name.

        Returns:
            Group names, or None when there is no container.
        """
        try:
            container = self._client.containers.get(container_name(user))
        except docker.errors.NotFound:
            return None
        return {g for g in container.labels.get(LABEL_GROUPS, "").split(",") if g}

    def start(
        self,
        user: dict[str, Any],
        image_ref: str,
        addons: Sequence[Addon] = (),
        groups: Sequence[dict[str, Any]] = (),
        ssh: SshAccess | None = None,
        user_env: Mapping[str, str] | None = None,
        gui: bool = True,
    ) -> None:
        """Start a fresh desktop container for a user, replacing any existing one.

        Also clears the user's ``stopped_by_admin`` flag.

        Args:
            user: User row from the database.
            image_ref: Docker image to run.
            addons: Add-ons enabled for this start.
            groups: Group rows whose folders are mounted.
            ssh: SSH gateway access, or None for no SSH.
            user_env: The user's own environment variables.
            gui: Whether the image has a desktop (only desktops are routed through Traefik).

        Raises:
            DesktopError: The account is disabled, an add-on's mount source is not installed,
                or the container could not start.
        """
        name = user["name"]
        if user["disabled"]:
            raise DesktopError("This account is disabled.")
        for addon in addons:
            for mount in addon.spec.mounts:
                if not (self._settings.root / mount.source).is_dir():
                    raise DesktopError(
                        f"{addon.spec.label} is not installed on the server yet. "
                        "Switch it off, or ask your administrator."
                    )
        with self._locks[name]:
            self._remove_container(name)
            spec = build_spec(
                user,
                image_ref,
                self._settings,
                self.gpu_vendor(),
                secrets.token_urlsafe(32),
                addons,
                groups,
                ssh,
                user_env=user_env,
                gui=gui,
            )
            network = self._ensure_network(name)
            if gui:
                self._connect_traefik(network)
            if ssh is not None:
                self._connect(network, self._settings.sshpiper_container)
            container = self._client.containers.create(
                spec.image,
                network=spec.network,
                networking_config={
                    spec.network: EndpointConfig(
                        version=self._client.api._version, mac_address=spec.mac
                    )
                },
                **spec.kwargs,
            )
            try:
                container.start()
            except docker.errors.APIError as exc:
                container.remove(force=True)
                raise DesktopError(f"The desktop could not start: {exc.explanation}") from exc
            self._db.execute("UPDATE users SET stopped_by_admin = false WHERE id = ?", [user["id"]])

    def stop(self, user: dict[str, Any]) -> None:
        """Remove a user's desktop container and network. The home folder stays.

        Args:
            user: User row from the database.
        """
        name = user["name"]
        with self._locks[name]:
            self._remove_container(name)
            self._remove_network(name)

    # --- helpers -----------------------------------------------------------------------------

    def _remove_container(self, name: str) -> None:
        """Stop the terminal and force-remove a user's desktop container, if there is one.

        Args:
            name: OrbitalWindow user name.
        """
        try:
            container = self._client.containers.get(container_name(name))
        except docker.errors.NotFound:
            return
        if container.status == "running":
            # Stop the terminal first, so its programs get SIGHUP rather than SIGKILL.
            try:
                stop_session(self._client, desktop_shell(name))
            except docker.errors.APIError as exc:
                log.warning("stop terminal of %s: %s", name, exc)
        with contextlib.suppress(docker.errors.NotFound):
            container.remove(force=True)

    def _ensure_network(self, name: str) -> Any:
        """Return a user's own network, creating it if needed.

        Args:
            name: OrbitalWindow user name.

        Returns:
            The Docker network.
        """
        net_name = network_name(name)
        nets = self._client.networks.list(names=[net_name])
        for net in nets:
            if net.name == net_name:
                return net
        return self._client.networks.create(
            net_name,
            driver="bridge",
            labels={LABEL_MANAGED: "true", LABEL_USER: name},
        )

    def _connect_traefik(self, network: Any) -> None:
        """Connect the Traefik container to a network, if not already connected.

        Args:
            network: Docker network.
        """
        self._connect(network, self._settings.traefik_container)

    def _connect(self, network: Any, container: str) -> None:
        """Connect a container to a network, if not already connected.

        Args:
            network: Docker network.
            container: Name of the container to connect.
        """
        network.reload()
        if any(c.name == container for c in network.containers):
            return
        try:
            network.connect(container)
        except docker.errors.APIError as exc:
            if "already exists" not in str(exc):
                raise

    def _remove_network(self, name: str) -> None:
        """Disconnect every container from a user's network and remove it.

        Args:
            name: OrbitalWindow user name.
        """
        for net in self._client.networks.list(names=[network_name(name)]):
            if net.name != network_name(name):
                continue
            net.reload()
            for container in net.containers:
                try:
                    net.disconnect(container, force=True)
                except docker.errors.APIError as exc:
                    log.warning("disconnect %s from %s: %s", container.name, net.name, exc)
            net.remove()

    def ensure_traefik_connected(self) -> int:
        """Reconnect Traefik to every user network (desktop or file manager) that has a member.

        Also connects sshpiper to desktop networks whose desktop has SSH access.

        Returns:
            The number of networks fixed.
        """
        fixed = 0
        traefik = self._settings.traefik_container
        for net in self._client.networks.list(filters={"label": f"{LABEL_MANAGED}=true"}):
            net.reload()
            names = {c.name for c in net.containers}
            user = net.attrs.get("Labels", {}).get(LABEL_USER)
            if user and names - {traefik} and traefik not in names:
                self._connect_traefik(net)
                fixed += 1
            # sshpiper only on desktop networks whose desktop has SSH access.
            sshpiper = self._settings.sshpiper_container
            if user and net.name == network_name(user) and sshpiper not in names:
                desktop = next((c for c in net.containers if c.name == container_name(user)), None)
                if desktop is not None and "sshpiper.username" in desktop.labels:
                    try:
                        self._connect(net, sshpiper)
                        fixed += 1
                    except docker.errors.APIError as exc:
                        log.warning("could not connect %s to %s: %s", sshpiper, net.name, exc)
        return fixed
