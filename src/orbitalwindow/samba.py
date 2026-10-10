# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Keep the Samba container in step with OrbitalWindow's users.

OrbitalWindow writes ``state/samba/users.json`` (names and UIDs only) and calls the helper
scripts baked into the Samba image. Passwords go to ``smbpasswd`` on stdin and are never stored.
"""

import json
import logging
import os
import socket
from pathlib import Path

import docker

from .db import Database

log = logging.getLogger(__name__)


class SambaError(RuntimeError):
    pass


class Samba:
    def __init__(self, client: docker.DockerClient, container: str, state_dir: Path) -> None:
        """Create the Samba controller.

        Args:
            client: Docker client used to exec into the Samba container.
            container: Name of the Samba container.
            state_dir: Folder for ``users.json``, shared with the container.
        """
        self._client = client
        self._container = container
        self._state_dir = state_dir

    def write_users(self, db: Database) -> None:
        """Write ``users.json`` (user names/UIDs and groups with members) atomically.

        Args:
            db: The application database.
        """
        users = db.all("SELECT name, uid_gid AS uid FROM users ORDER BY name")
        groups = []
        for group in db.all("SELECT id, name, gid FROM groups ORDER BY name"):
            members = db.all(
                "SELECT u.name FROM group_members m JOIN users u ON u.id = m.user_id "
                "WHERE m.group_id = ? ORDER BY u.name",
                [group["id"]],
            )
            groups.append(
                {
                    "name": group["name"],
                    "gid": group["gid"],
                    "members": [member["name"] for member in members],
                }
            )
        self._state_dir.mkdir(parents=True, exist_ok=True)
        path = self._state_dir / "users.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"users": users, "groups": groups}, indent=2))
        os.chmod(tmp, 0o644)
        tmp.replace(path)

    def _exec(self, cmd: list[str], stdin: str | None = None) -> str:
        """Run a command in the Samba container.

        Args:
            cmd: The command and its arguments.
            stdin: Text to send on stdin, or None.

        Returns:
            The command's output.

        Raises:
            SambaError: If the command exits with a non-zero code.
        """
        container = self._client.containers.get(self._container)
        if stdin is None:
            code, out = container.exec_run(cmd)
            text = out.decode(errors="replace") if out else ""
        else:
            code, text = self._exec_stdin(container.id, cmd, stdin)
        if code != 0:
            raise SambaError(f"{' '.join(cmd[:2])} failed ({code}): {text.strip()[-500:]}")
        return text

    def _exec_stdin(self, container_id: str, cmd: list[str], stdin: str) -> tuple[int, str]:
        """Run a command in a container, feeding text on stdin through the exec socket.

        Args:
            container_id: The container's ID.
            cmd: The command and its arguments.
            stdin: Text to send on stdin.

        Returns:
            The exit code (1 when unknown) and the output.
        """
        api = self._client.api
        exec_id = api.exec_create(container_id, cmd, stdin=True, stdout=True, stderr=True)["Id"]
        sock = api.exec_start(exec_id, socket=True)
        raw = getattr(sock, "_sock", sock)
        raw.sendall(stdin.encode())
        raw.shutdown(socket.SHUT_WR)
        chunks = []
        while data := raw.recv(4096):
            chunks.append(data)
        raw.close()
        code = api.exec_inspect(exec_id)["ExitCode"]
        return code if code is not None else 1, _demux(b"".join(chunks))

    def sync(self, db: Database) -> None:
        """Rewrite users.json and let the container create Unix users and shares, then reload.

        Args:
            db: The application database.

        Raises:
            SambaError: If the sync command fails.
        """
        self.write_users(db)
        self._exec(["ow-sync"])

    def set_password(self, name: str, password: str) -> None:
        """Set a user's Samba password.

        Args:
            name: OrbitalWindow user name.
            password: The new password (sent on stdin, never stored).

        Raises:
            SambaError: If the password could not be set.
        """
        self._exec(["ow-passwd", name], stdin=password + "\n")

    def set_enabled(self, name: str, enabled: bool) -> None:
        """Enable or disable a user's Samba account.

        Args:
            name: OrbitalWindow user name.
            enabled: True to enable, False to disable.

        Raises:
            SambaError: If ``smbpasswd`` fails.
        """
        self._exec(["smbpasswd", "-e" if enabled else "-d", name])

    def remove_user(self, name: str, db: Database) -> None:
        """Remove a user's Samba account (a failure is only logged), then sync.

        Args:
            name: OrbitalWindow user name.
            db: The application database.

        Raises:
            SambaError: If the sync fails.
        """
        try:
            self._exec(["smbpasswd", "-x", name])
        except SambaError as exc:
            log.warning("smbpasswd -x %s: %s", name, exc)
        self.sync(db)


def _demux(data: bytes) -> str:
    """Strip Docker's multiplexed-stream framing (8-byte headers) from exec output.

    Args:
        data: The raw exec output.

    Returns:
        The decoded output; the data as-is when it is not framed.
    """
    out = bytearray()
    offset = 0
    while (
        offset + 8 <= len(data)
        and data[offset] in (0, 1, 2)
        and data[offset + 1 : offset + 4] == b"\0\0\0"
    ):
        size = int.from_bytes(data[offset + 4 : offset + 8], "big")
        out += data[offset + 8 : offset + 8 + size]
        offset += 8 + size
    if offset == 0:
        return data.decode(errors="replace")
    return out.decode(errors="replace")
