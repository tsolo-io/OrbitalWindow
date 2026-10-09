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

    def start(self, user: dict[str, Any], image_ref: str, addons: Sequence[Addon] = ()) -> None:
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
            )
            network = self._ensure_network(name)
            self._connect_traefik(network)
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
        network.reload()
        traefik = self._settings.traefik_container
        if any(c.name == traefik for c in network.containers):
            return
        try:
            network.connect(traefik)
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
        """Reconnect Traefik to every user network that has a running desktop. Returns fixes."""
        fixed = 0
        for net in self._client.networks.list(filters={"label": f"{LABEL_MANAGED}=true"}):
            net.reload()
            names = {c.name for c in net.containers}
            user = net.attrs.get("Labels", {}).get(LABEL_USER)
            if (
                user
                and container_name(user) in names
                and self._settings.traefik_container not in names
            ):
                self._connect_traefik(net)
                fixed += 1
        return fixed
