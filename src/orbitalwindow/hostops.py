# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""File operations under ``$OW_ROOT`` that need root (owners, ACLs, other users' files).

The service itself does not run as root. These run in a short-lived helper container that only
mounts ``$OW_ROOT``. Arguments are passed as argv (never through a shell string built from input).
"""

import io
import logging
import shlex

import docker

log = logging.getLogger(__name__)

BASE_IMAGE = "alpine:3.22"
HELPER_IMAGE = "ow-helper:1"  # bump the tag when HELPER_DOCKERFILE changes
HELPER_DOCKERFILE = f"""FROM {BASE_IMAGE}
RUN apk add --no-cache acl findutils coreutils
LABEL ow.managed="true" ow.role="helper-image"
"""
# Path $OW_ROOT is mounted at inside the helper.
MNT = "/ow"

SCRATCH_README = (
    "This is the shared scratch folder. Everyone can add files here; normally only the owner of "
    "a file can delete it. It is not private: anyone who uses sudo in their desktop can read, "
    "change or delete any file here.\\n\\nFiles that have not been changed for %s days are deleted "
    "automatically every night. Keep anything important in your home folder or a group folder.\\n"
)

# Fixed scripts; user-controlled values arrive as positional parameters ($1, $2, …).
_LAYOUT = f"""
set -eu
mkdir -p /ow/users /ow/groups /ow/shared/data /ow/shared/scratch /ow/state/samba /ow/state/ssh \\
         /ow/state/files /ow/state/files-config
chmod 755 /ow/users /ow/groups /ow/shared /ow/shared/data
chmod 1777 /ow/shared/scratch
chown "$1:$1" /ow/state /ow/state/samba /ow/state/ssh /ow/state/files-config
chmod 700 /ow/state /ow/state/ssh
chmod 755 /ow/state/samba /ow/state/files /ow/state/files-config
printf '{SCRATCH_README}' "$2" > /ow/shared/scratch/README-auto-clean.txt
chmod 644 /ow/shared/scratch/README-auto-clean.txt
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

# Group folder: owner root:<gid>, setgid, default ACL so new files stay group-writable.
_GROUP_DIR = """
set -eu
mkdir -p "/ow/groups/$1"
chown "root:$2" "/ow/groups/$1"
setfacl -m g::rwx "/ow/groups/$1"
setfacl -d -m g::rwX -m o::--- "/ow/groups/$1"
# Last, because changing ACLs/mode can clear the setgid bit.
chmod 2770 "/ow/groups/$1"
"""

_REMOVE_GROUP_DIR = """
set -eu
rm -rf "/ow/groups/$1"
"""

# Per-user file manager data (database, cache), owned by the user.
_FILES_DATA = """
set -eu
mkdir -p "/ow/state/files/$1"
chown "$2:$2" "/ow/state/files/$1"
chmod 700 "/ow/state/files/$1"
"""

_REMOVE_FILES_DATA = """
set -eu
rm -rf "/ow/state/files/$1" "/ow/state/files-config/$1.yaml"
"""

# Disk usage in bytes of every home, group folder and the scratch folder, one line each.
_DU = """
set -u
cd /ow
for d in users/* groups/* shared/scratch; do
  [ -d "$d" ] || continue
  printf '%s\\t' "$d"
  du -sxb "$d" 2>/dev/null | cut -f1 || echo 0
done
"""

# Delete scratch files untouched for $1 days, then empty folders; keep the README.
_CLEAN_SCRATCH = """
set -eu
cd /ow/shared/scratch
find . -xdev -mindepth 1 -type f ! -name README-auto-clean.txt -mtime +"$1" -print -delete
find . -xdev -mindepth 1 -depth -type d -empty -mtime +"$1" -print -delete
"""


class HostOps:
    def __init__(self, client: docker.DockerClient, root: str) -> None:
        """Create the helper runner.

        Args:
            client: Docker client used to build and run the helper container.
            root: Host path of ``$OW_ROOT``, mounted into the helper.
        """
        self._client = client
        self._root = root
        self._image_ready = False

    def _ensure_image(self) -> None:
        """Build the helper image unless it already exists."""
        if self._image_ready:
            return
        try:
            self._client.images.get(HELPER_IMAGE)
        except docker.errors.ImageNotFound:
            log.info("building %s", HELPER_IMAGE)
            for _ in self._client.api.build(
                fileobj=io.BytesIO(HELPER_DOCKERFILE.encode()),
                tag=HELPER_IMAGE,
                rm=True,
                pull=True,
                decode=True,
            ):
                pass
            self._client.images.get(HELPER_IMAGE)  # raises if the build failed
        self._image_ready = True

    def _run(self, script: str, *args: str, read_all: bool = False) -> str:
        """Run a fixed shell script in a throw-away helper container.

        Args:
            script: The shell script (``sh -c``).
            *args: Positional parameters for the script (``$1``, ``$2``, …).
            read_all: Also grant ``DAC_READ_SEARCH`` so the script can read every file.

        Returns:
            The script's output.
        """
        self._ensure_image()
        cmd = ["sh", "-c", script, "ow-helper", *args]
        log.debug("helper: %s", shlex.join(args))
        # FSETID: keep the setgid bit on group folders when root (not a member) sets the mode.
        caps = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID"]
        if read_all:
            caps.append("DAC_READ_SEARCH")
        out = self._client.containers.run(
            HELPER_IMAGE,
            cmd,
            remove=True,
            network_mode="none",
            cap_drop=["ALL"],
            cap_add=caps,
            security_opt=["no-new-privileges"],
            volumes={self._root: {"bind": MNT, "mode": "rw"}},
            labels={"ow.managed": "true", "ow.role": "helper"},
        )
        return out.decode(errors="replace") if out else ""

    def prepare_layout(self, service_uid: int, scratch_days: int = 30) -> None:
        """Create the folder layout under ``$OW_ROOT`` with its owners and modes.

        Args:
            service_uid: UID the service runs as; it owns the state folders.
            scratch_days: Days after which scratch files are deleted (for the README).
        """
        self._run(_LAYOUT, str(service_uid), str(scratch_days))

    def create_home(self, name: str, uid: int) -> None:
        """Create a user's home folder.

        Args:
            name: OrbitalWindow user name.
            uid: The user's UID/GID, which owns the folder.
        """
        self._run(_HOME, name, str(uid))

    def remove_home(self, name: str) -> None:
        """Delete a user's home folder and everything in it.

        Args:
            name: OrbitalWindow user name.
        """
        self._run(_REMOVE_HOME, name)

    def create_group_dir(self, name: str, gid: int) -> None:
        """Create a group folder (setgid, with a default ACL that keeps files group-writable).

        Args:
            name: The group name.
            gid: The group's GID.
        """
        self._run(_GROUP_DIR, name, str(gid))

    def remove_group_dir(self, name: str) -> None:
        """Delete a group folder and everything in it.

        Args:
            name: The group name.
        """
        self._run(_REMOVE_GROUP_DIR, name)

    def create_files_data(self, name: str, uid: int) -> None:
        """Create a user's file manager data folder.

        Args:
            name: OrbitalWindow user name.
            uid: The user's UID/GID, which owns the folder.
        """
        self._run(_FILES_DATA, name, str(uid))

    def remove_files_data(self, name: str) -> None:
        """Delete a user's file manager data folder and config file.

        Args:
            name: OrbitalWindow user name.
        """
        self._run(_REMOVE_FILES_DATA, name)

    def disk_usage(self) -> dict[str, int]:
        """Measure the disk usage of every home, group folder and the scratch folder.

        Returns:
            ``{"users/bob": bytes, "groups/labx": bytes, "shared/scratch": bytes}``.
        """
        result: dict[str, int] = {}
        for line in self._run(_DU, read_all=True).splitlines():
            path, _, size = line.partition("\t")
            if size.strip().isdigit():
                result[path] = int(size)
        return result

    def clean_scratch(self, days: int) -> list[str]:
        """Delete scratch files untouched for ``days``, then empty folders.

        Args:
            days: Age in days after which an unchanged file is deleted.

        Returns:
            The removed paths.
        """
        out = self._run(_CLEAN_SCRATCH, str(days))
        return [line for line in out.splitlines() if line]
