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
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import docker
from docker.types import DeviceRequest, EndpointConfig

from .addons import Addon
from .config import Settings
from .db import Database

log = logging.getLogger(__name__)

LABEL_MANAGED = "ow.managed"
LABEL_USER = "ow.user"
LABEL_ROLE = "ow.role"
LABEL_ADDONS = "ow.addons"
LABEL_GROUPS = "ow.groups"
DESKTOP_PORT = 3000


class GpuVendor(StrEnum):
    NVIDIA = "nvidia"
    AMD = "amd"
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
    return f"ow-{user}"


def network_name(user: str) -> str:
    return f"ow-net-{user}"


def desktop_path(user: str) -> str:
    return f"/desktop/{user}/"


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
) -> DesktopSpec:
    """The container for ``user``. Add-on mounts and environment are applied here (no rebuild)."""
    name = user["name"]
    cname = container_name(name)
    net = network_name(name)
    router = cname
    path = desktop_path(name)
    basic = base64.b64encode(f"{name}:{secret}".encode()).decode()

    labels = {
        LABEL_MANAGED: "true",
        LABEL_USER: name,
        LABEL_ROLE: "desktop",
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
        # Add-ons active at start; used to find desktops that use a revoked add-on's mounts.
        LABEL_ADDONS: ",".join(a.key for a in addons),
    }

    uid = str(user["uid_gid"])
    environment = {
        "PUID": uid,
        "PGID": uid,
        "TZ": settings.tz,
        "TITLE": f"{user['display_name']} – OrbitalWindow",
        "CUSTOM_USER": name,
        "PASSWORD": secret,
        "SUBFOLDER": path,
        "PIXELFLUX_WAYLAND": "false",
        "START_DOCKER": "false",
    }

    root = settings.root
    volumes = {
        str(root / "users" / name): {"bind": "/config", "mode": "rw"},
        str(root / "shared" / "scratch"): {"bind": "/scratch", "mode": "rw"},
        str(root / "shared" / "data"): {"bind": "/shared", "mode": "ro"},
    }
    for g in groups:
        volumes[str(root / "groups" / g["name"])] = {"bind": f"/groups/{g['name']}", "mode": "rw"}
    if ssh is not None:
        # The only key the desktop's sshd accepts; the gateway checks the user's own keys.
        volumes[str(ssh.upstream_pub)] = {"bind": "/etc/ow/upstream.pub", "mode": "ro"}
        labels.update(ssh.labels)
    # The init hook creates these groups and adds abc to them (s6 keeps supplementary groups).
    environment["OW_GROUPS"] = " ".join(f"{g['name']}:{g['gid']}" for g in groups)
    labels[LABEL_GROUPS] = ",".join(g["name"] for g in groups)
    for addon in addons:
        for m in addon.spec.mounts:
            mode = "ro" if m.read_only else "rw"
            volumes[str(root / m.source)] = {"bind": m.target, "mode": mode}
        for key, value in addon.spec.env.items():
            environment.setdefault(key, value)

    devices: list[str] = []
    device_requests: list[DeviceRequest] = []
    if gpu_vendor is GpuVendor.NVIDIA:
        devices.append("/dev/dri:/dev/dri:rwm")
        device_requests.append(DeviceRequest(count=-1, capabilities=[["gpu"]]))
        environment["NVIDIA_DRIVER_CAPABILITIES"] = "all"
    elif gpu_vendor is GpuVendor.AMD:
        devices += ["/dev/dri:/dev/dri:rwm", "/dev/kfd:/dev/kfd:rwm"]

    kwargs: dict[str, Any] = {
        "name": cname,
        "hostname": cname,
        "labels": labels,
        "environment": environment,
        "volumes": volumes,
        "devices": devices,
        "device_requests": device_requests,
        "cap_drop": ["NET_RAW"],
        "shm_size": "2g",
        "restart_policy": {"Name": "unless-stopped"},
    }
    if user.get("cpus"):
        kwargs["nano_cpus"] = int(user["cpus"] * 1e9)
    if user.get("mem_gb"):
        kwargs["mem_limit"] = int(user["mem_gb"] * 1024**3)
    return DesktopSpec(cname, image_ref, net, user["mac"], kwargs)


def _state_of(state: dict[str, Any]) -> DesktopState:
    """Map Docker's ``State`` to ours. Running counts only once the healthcheck has passed."""
    status = state.get("Status")
    if status == "running":
        health = (state.get("Health") or {}).get("Status")
        return DesktopState.STARTING if health == "starting" else DesktopState.RUNNING
    if status in ("exited", "dead"):
        return DesktopState.ERROR
    return DesktopState.STARTING  # created, restarting, paused


class Desktops:
    def __init__(self, client: docker.DockerClient, db: Database, settings: Settings) -> None:
        self._client = client
        self._db = db
        self._settings = settings
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    # --- queries -----------------------------------------------------------------------------

    def gpu_vendor(self) -> GpuVendor:
        return GpuVendor(self._db.get_setting("gpu_vendor", GpuVendor.NVIDIA))

    def state(self, user: str) -> DesktopState:
        try:
            container = self._client.containers.get(container_name(user))
        except docker.errors.NotFound:
            return DesktopState.STOPPED
        return _state_of(container.attrs.get("State", {}))

    def states(self) -> dict[str, DesktopState]:
        """State of every managed desktop, in one Docker call (for admin pages and reconcile)."""
        result: dict[str, DesktopState] = {}
        for c in self._client.containers.list(
            all=True, filters={"label": [f"{LABEL_MANAGED}=true", f"{LABEL_ROLE}=desktop"]}
        ):
            user = c.labels.get(LABEL_USER)
            if user:
                result[user] = _state_of(c.attrs.get("State", {}))
        return result

    # --- lifecycle ---------------------------------------------------------------------------

    def addons_in_use(self, user: str) -> set[str]:
        """Keys of the add-ons the running desktop was started with."""
        try:
            container = self._client.containers.get(container_name(user))
        except docker.errors.NotFound:
            return set()
        return {k for k in container.labels.get(LABEL_ADDONS, "").split(",") if k}

    def groups_in_use(self, user: str) -> set[str] | None:
        """Groups the running desktop was started with; None when it is not running."""
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
    ) -> None:
        name = user["name"]
        if user["disabled"]:
            raise DesktopError("This account is disabled.")
        for addon in addons:
            for m in addon.spec.mounts:
                if not (self._settings.root / m.source).is_dir():
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
            )
            network = self._ensure_network(name)
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
            except docker.errors.APIError as e:
                container.remove(force=True)
                raise DesktopError(f"The desktop could not start: {e.explanation}") from e
            self._db.execute("UPDATE users SET stopped_by_admin = false WHERE id = ?", [user["id"]])

    def stop(self, user: dict[str, Any]) -> None:
        name = user["name"]
        with self._locks[name]:
            self._remove_container(name)
            self._remove_network(name)

    # --- helpers -----------------------------------------------------------------------------

    def _remove_container(self, name: str) -> None:
        with contextlib.suppress(docker.errors.NotFound):
            self._client.containers.get(container_name(name)).remove(force=True)

    def _ensure_network(self, name: str) -> Any:
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
        self._connect(network, self._settings.traefik_container)

    def _connect(self, network: Any, container: str) -> None:
        network.reload()
        if any(c.name == container for c in network.containers):
            return
        try:
            network.connect(container)
        except docker.errors.APIError as e:
            if "already exists" not in str(e):
                raise

    def _remove_network(self, name: str) -> None:
        for net in self._client.networks.list(names=[network_name(name)]):
            if net.name != network_name(name):
                continue
            net.reload()
            for c in net.containers:
                try:
                    net.disconnect(c, force=True)
                except docker.errors.APIError as e:
                    log.warning("disconnect %s from %s: %s", c.name, net.name, e)
            net.remove()

    def ensure_traefik_connected(self) -> int:
        """Reconnect Traefik to every user network (desktop or file manager) that has a member.
        Returns the number of networks fixed."""
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
                    except docker.errors.APIError as e:
                        log.warning("could not connect %s to %s: %s", sshpiper, net.name, e)
        return fixed
