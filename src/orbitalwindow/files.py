# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Per-user web file manager: FileBrowser Quantum in ``ow-files-<user>``.

It runs as the user's UID, mounts the same folders as the desktop and trusts the ``X-OW-User``
header set by Traefik after forwardAuth. It lives on its own network ``ow-fnet-<user>`` that only
Traefik joins, so nothing else (not even the user's desktop) can send it a forged header.
"""

import contextlib
import hashlib
import logging
import secrets
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker
import yaml

from .config import Settings
from .desktops import LABEL_MANAGED, LABEL_ROLE, LABEL_USER

log = logging.getLogger(__name__)

FILES_IMAGE = "gtstef/filebrowser:2.0.0-stable-slim"
FILES_PORT = 8080
LABEL_SPEC = "ow.spec"  # hash of the container spec; a change means recreate
# FileBrowser's built-in admin. The underscore makes it a name OrbitalWindow never issues.
FB_ADMIN = "ow_fb_admin"
FULL_ACCESS = {
    "view": True,
    "download": True,
    "modify": True,
    "delete": True,
    "create": True,
    "configured": True,
}


def files_container_name(user: str) -> str:
    return f"ow-files-{user}"


def files_network_name(user: str) -> str:
    return f"ow-fnet-{user}"


def files_path(user: str) -> str:
    return f"/files/{user}/"


def _source(path: str, name: str, *, read_only: bool = False) -> dict[str, Any]:
    access = {k: (v if k in ("view", "download", "configured") or not read_only else False)
              for k, v in FULL_ACCESS.items()}  # fmt: skip
    return {
        "path": path,
        "name": name,
        "config": {
            "private": True,  # no share links
            "readOnly": read_only,
            "defaultEnabled": True,
            "defaultPermissions": access,
        },
    }


def files_config(user: str, groups: list[str], admin_password: str) -> dict[str, Any]:
    """FileBrowser Quantum config: one source per mount, proxy-header login, no share links."""
    sources = [_source("/srv/home", "Home")]
    sources += [_source(f"/srv/groups/{g}", f"Group {g}") for g in groups]
    sources.append(_source("/srv/scratch", "Scratch"))
    sources.append(_source("/srv/shared", "Shared data", read_only=True))
    return {
        "http": {
            "port": FILES_PORT,
            "baseURL": files_path(user),
            "disableWebDAV": True,
            "trustProxyHeaders": True,
        },
        "server": {
            "disableUpdateCheck": True,
            "cacheDir": "/home/filebrowser/data/tmp",
            "database": {"path": "/home/filebrowser/data/database.sqlite"},
            # Group-writable, so files created in group folders stay editable by the members
            # (FileBrowser sets the mode explicitly, overriding the folder's default ACL). Homes
            # are private (750) and use the user's own group, so this opens nothing there.
            "filesystem": {"createFilePermission": "664", "createDirectoryPermission": "2775"},
            "sources": sources,
        },
        "auth": {
            "adminUsername": FB_ADMIN,
            "adminPassword": admin_password,
            "methods": {
                "proxy": {"enabled": True, "header": "X-OW-User"},
                "password": {"enabled": False},
            },
        },
        "userDefaults": {
            "account": {
                "permissions": {"api": False, "admin": False, "share": False, "realtime": False},
                "loginMethod": "proxy",
            },
        },
        "frontend": {"name": "OrbitalWindow files", "disableDefaultLinks": True},
    }


@dataclass(frozen=True)
class FilesSpec:
    name: str
    network: str
    kwargs: dict[str, Any]
    spec_hash: str


def build_files_spec(
    user: dict[str, Any], groups: list[dict[str, Any]], settings: Settings
) -> FilesSpec:
    name = user["name"]
    cname = files_container_name(name)
    net = files_network_name(name)
    root = settings.root
    uid = user["uid_gid"]
    volumes = {
        str(root / "users" / name): {"bind": "/srv/home", "mode": "rw"},
        str(root / "shared" / "scratch"): {"bind": "/srv/scratch", "mode": "rw"},
        str(root / "shared" / "data"): {"bind": "/srv/shared", "mode": "ro"},
        str(root / "state" / "files" / name): {"bind": "/home/filebrowser/data", "mode": "rw"},
        str(root / "state" / "files-config" / f"{name}.yaml"): {
            "bind": "/home/filebrowser/config.yaml",
            "mode": "ro",
        },
    }
    for g in groups:
        target = f"/srv/groups/{g['name']}"
        volumes[str(root / "groups" / g["name"])] = {"bind": target, "mode": "rw"}
    router = cname
    path = files_path(name)
    labels = {
        LABEL_MANAGED: "true",
        LABEL_USER: name,
        LABEL_ROLE: "files",
        "traefik.enable": "true",
        "traefik.docker.network": net,
        f"traefik.http.routers.{router}.rule": f"Path(`{path[:-1]}`) || PathPrefix(`{path}`)",
        f"traefik.http.routers.{router}.entrypoints": settings.entrypoint,
        f"traefik.http.routers.{router}.tls": "true",
        f"traefik.http.routers.{router}.service": router,
        f"traefik.http.routers.{router}.middlewares": "ow-files-slash@docker,ow-auth-files@docker",
        f"traefik.http.services.{router}.loadbalancer.server.port": str(FILES_PORT),
    }
    kwargs: dict[str, Any] = {
        "name": cname,
        "hostname": cname,
        "user": f"{uid}:{uid}",
        "group_add": [str(g["gid"]) for g in groups],
        "environment": {"FILEBROWSER_CONFIG": "/home/filebrowser/config.yaml", "TZ": settings.tz},
        "volumes": volumes,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "read_only": True,
        "tmpfs": {"/tmp": ""},
        "mem_limit": 512 * 1024**2,
        # The image's own check assumes port 80 and "/"; we run unprivileged on 8080 under
        # /files/<user>/. Traefik only routes to healthy containers.
        "healthcheck": {
            "test": [
                "CMD",
                "curl",
                "-fsS",
                "-o",
                "/dev/null",
                f"http://localhost:{FILES_PORT}{path}health",
            ],
            "interval": 30 * 10**9,
            "timeout": 3 * 10**9,
            "start_period": 15 * 10**9,
            "start_interval": 2 * 10**9,
            "retries": 3,
        },
        "restart_policy": {"Name": "unless-stopped"},
    }
    digest = hashlib.sha256(
        repr(
            (
                FILES_IMAGE,
                sorted(volumes.items()),
                kwargs["group_add"],
                labels,
                # The generated config (without its random password): a change recreates.
                files_config(name, [g["name"] for g in groups], ""),
                kwargs["healthcheck"],
            )
        ).encode()
    ).hexdigest()[:16]
    labels[LABEL_SPEC] = digest
    kwargs["labels"] = labels
    return FilesSpec(cname, net, kwargs, digest)


class FileManagers:
    def __init__(self, client: docker.DockerClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    def _write_config(self, user: str, groups: list[str]) -> None:
        path: Path = self._settings.state_dir / "files-config" / f"{user}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        config = files_config(user, groups, secrets.token_urlsafe(24))
        text = yaml.safe_dump(config, sort_keys=False)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.chmod(0o644)  # read by the user's UID inside the container; holds no user secret
        tmp.replace(path)

    def ensure(
        self, user: dict[str, Any], groups: list[dict[str, Any]], *, recreate: bool = False
    ) -> bool:
        """Make sure ``ow-files-<user>`` runs with the current spec. Returns True if (re)created."""
        name = user["name"]
        spec = build_files_spec(user, groups, self._settings)
        with self._locks[name]:
            try:
                current = self._client.containers.get(spec.name)
            except docker.errors.NotFound:
                current = None
            if current is not None and not recreate:
                same = current.labels.get(LABEL_SPEC) == spec.spec_hash
                if same and current.status == "running":
                    return False
                if same:
                    current.start()
                    return False
            if current is not None:
                current.remove(force=True)
            self._write_config(name, [g["name"] for g in groups])
            network = self._ensure_network(name)
            self._connect_traefik(network)
            self._ensure_image()
            container = self._client.containers.create(
                FILES_IMAGE, network=spec.network, **spec.kwargs
            )
            container.start()
            return True

    def stop(self, name: str) -> None:
        with contextlib.suppress(docker.errors.NotFound):
            self._client.containers.get(files_container_name(name)).stop(timeout=5)

    def remove(self, name: str) -> None:
        with self._locks[name]:
            with contextlib.suppress(docker.errors.NotFound):
                self._client.containers.get(files_container_name(name)).remove(force=True)
            for net in self._client.networks.list(names=[files_network_name(name)]):
                if net.name != files_network_name(name):
                    continue
                net.reload()
                for c in net.containers:
                    with contextlib.suppress(docker.errors.APIError):
                        net.disconnect(c, force=True)
                net.remove()

    def _ensure_image(self) -> None:
        try:
            self._client.images.get(FILES_IMAGE)
        except docker.errors.ImageNotFound:
            log.info("pulling %s", FILES_IMAGE)
            self._client.images.pull(FILES_IMAGE)

    def _ensure_network(self, name: str) -> Any:
        net_name = files_network_name(name)
        for net in self._client.networks.list(names=[net_name]):
            if net.name == net_name:
                return net
        return self._client.networks.create(
            net_name, driver="bridge", labels={LABEL_MANAGED: "true", LABEL_USER: name}
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
