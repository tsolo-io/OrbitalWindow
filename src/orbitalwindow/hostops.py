# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""File operations under ``$OW_ROOT`` that need root (chown to a user's UID, removing their files).

The service itself does not run as root. These run in a short-lived helper container that only
mounts ``$OW_ROOT``. Arguments are passed as argv (never through a shell string built from input).
"""

import logging
import shlex

import docker

log = logging.getLogger(__name__)

HELPER_IMAGE = "alpine:3.22"
# Path $OW_ROOT is mounted at inside the helper.
MNT = "/ow"

# Fixed script; user-controlled values arrive as positional parameters ($1, $2, …).
_LAYOUT = """
set -eu
mkdir -p /ow/users /ow/groups /ow/shared/data /ow/shared/scratch /ow/state/samba /ow/state/ssh
chmod 755 /ow/users /ow/groups /ow/shared /ow/shared/data
chmod 1777 /ow/shared/scratch
chown "$1:$1" /ow/state /ow/state/samba /ow/state/ssh
chmod 700 /ow/state /ow/state/ssh
chmod 755 /ow/state/samba
"""

_HOME = """
set -eu
mkdir -p "/ow/users/$1"
chown "$2:$2" "/ow/users/$1"
chmod 750 "/ow/users/$1"
"""

_REMOVE_HOME = """
set -eu
rm -rf "/ow/users/$1"
"""


class HostOps:
    def __init__(self, client: docker.DockerClient, root: str) -> None:
        self._client = client
        self._root = root

    def _run(self, script: str, *args: str) -> None:
        cmd = ["sh", "-c", script, "ow-helper", *args]
        log.debug("helper: %s", shlex.join(args))
        try:
            self._client.images.get(HELPER_IMAGE)
        except docker.errors.ImageNotFound:
            self._client.images.pull(HELPER_IMAGE)
        self._client.containers.run(
            HELPER_IMAGE,
            cmd,
            remove=True,
            network_mode="none",
            cap_drop=["ALL"],
            cap_add=["CHOWN", "DAC_OVERRIDE", "FOWNER"],
            security_opt=["no-new-privileges"],
            volumes={self._root: {"bind": MNT, "mode": "rw"}},
            labels={"ow.managed": "true", "ow.role": "helper"},
        )

    def prepare_layout(self, service_uid: int) -> None:
        self._run(_LAYOUT, str(service_uid))

    def create_home(self, name: str, uid: int) -> None:
        self._run(_HOME, name, str(uid))

    def remove_home(self, name: str) -> None:
        self._run(_REMOVE_HOME, name)
