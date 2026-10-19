# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""A terminal in a running container that keeps running without a browser (ADR 0030).

The shell runs in a tmux session (``ow``) inside the container, as the container's own user
(never root). The browser page attaches to it with a ``docker exec`` that has a TTY; its socket is
read and written with asyncio, so a page needs no thread. Closing the page only detaches: programs
keep running until the user presses **Stop terminal** or the container stops.

Docker keeps an exec running after its connection closes, so the attaching tmux client carries a
token in its environment and closing the page sends it SIGHUP (which detaches it). The tmux server
is started by a separate exec without the token, so the hang-up never reaches the shell.

Images without tmux (a desktop not rebuilt yet) get a plain shell that ends with the page.
"""

import asyncio
import contextlib
import logging
import secrets
import shlex
import socket
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)

TOKEN_VAR = "OW_TERMINAL"
SESSION = "ow"
CONF = "/tmp/ow-tmux.conf"
# bash where the image has it, else sh; both as login shells so the profile is read.
SHELL = "if command -v bash >/dev/null; then exec bash -l; else exec sh -l; fi"
# Mouse on: the wheel scrolls tmux's history (xterm.js can't see it). The newest page sets the
# size. The user's own ~/.tmux.conf comes last.
TMUX_CONF = """\
set -g mouse on
set -g history-limit 50000
set -g window-size latest
set -g status-right ' Close the tab: keeps running '
source-file -q ~/.tmux.conf
"""
NO_TMUX = 3  # exit code of the start command when the image has no tmux
READ_SIZE = 65536
STOP_GRACE = 2  # seconds programs get to exit after the terminal is stopped


class SessionStart(StrEnum):
    STARTED = "started"  # a new tmux session was created
    RUNNING = "running"  # the session was already running
    NO_TMUX = "no-tmux"  # the image has no tmux: a plain shell that ends with the page


@dataclass(frozen=True)
class ShellTarget:
    """Where and as whom a terminal's shell runs.

    Attributes:
        container: Name or id of the running container.
        user: User the shell runs as.
        home: Home folder; the shell starts there.
    """

    container: str
    user: str
    home: str


def start_command(rows: int, cols: int) -> list[str]:
    """Return the command that starts the tmux session unless it already runs.

    Exits 0 when it started the session, 1 when it was running, ``NO_TMUX`` without tmux.

    Args:
        rows: Initial height in lines.
        cols: Initial width in characters.

    Returns:
        The command for ``exec_run``.
    """
    new = shlex.join(
        ["tmux", "-f", CONF, "new-session", "-d", "-s", SESSION, "-x", str(cols), "-y", str(rows)]
    )
    script = (
        f"command -v tmux >/dev/null || exit {NO_TMUX}; "
        f"tmux has-session -t {SESSION} 2>/dev/null && exit 1; "
        f"printf %s {shlex.quote(TMUX_CONF)} > {CONF}; "
        f'cd "$HOME"; exec {new} {shlex.quote(SHELL)}'
    )
    return ["sh", "-c", script]


def attach_kwargs(target: ShellTarget, token: str, tmux: bool) -> dict[str, Any]:
    """Return the ``exec_create`` arguments that connect a page to the shell.

    Args:
        target: Where and as whom the shell runs.
        token: Value of ``OW_TERMINAL`` that marks this page's processes.
        tmux: Attach to the tmux session; False runs a plain shell instead.

    Returns:
        Keyword arguments for ``APIClient.exec_create``.
    """
    cmd = ["tmux", "attach-session", "-t", SESSION] if tmux else ["sh", "-c", SHELL]
    return {
        "container": target.container,
        "cmd": cmd,
        "stdin": True,
        "stdout": True,
        "stderr": True,
        "tty": True,
        "user": target.user,
        "workdir": target.home,
        "environment": _env(target) | {TOKEN_VAR: token},
    }


def hangup_command(token: str) -> list[str]:
    """Return a command that sends SIGHUP to every process of one page.

    The processes are found by ``OW_TERMINAL=<token>`` in ``/proc/<pid>/environ``; the command
    itself does not carry the token.

    Args:
        token: The page's token (hex, so it is safe in the shell script).

    Returns:
        The command for ``exec_run``.
    """
    script = (
        "for env in /proc/[0-9]*/environ; do "
        f"if tr '\\0' '\\n' < \"$env\" 2>/dev/null | grep -qx '{TOKEN_VAR}={token}'; then "
        'pid=${env#/proc/}; kill -HUP "${pid%/environ}" 2>/dev/null; fi; done; true'
    )
    return ["sh", "-c", script]


def _env(target: ShellTarget) -> dict[str, str]:
    """Return the environment of the shell and of tmux commands.

    Args:
        target: Where and as whom the shell runs.

    Returns:
        ``HOME`` and ``TERM``.
    """
    return {"HOME": target.home, "TERM": "xterm-256color"}


def _run(client: Any, target: ShellTarget, cmd: list[str]) -> int:
    """Run a short command in the container as the shell's user (blocking).

    Args:
        client: Docker client.
        target: Where and as whom the shell runs.
        cmd: The command.

    Returns:
        Its exit code.
    """
    container = client.containers.get(target.container)
    result = container.exec_run(cmd, user=target.user, environment=_env(target))
    return result.exit_code


def ensure_session(client: Any, target: ShellTarget, rows: int, cols: int) -> SessionStart:
    """Start the tmux session if it isn't running (blocking).

    Args:
        client: Docker client.
        target: Where and as whom the shell runs.
        rows: Initial height in lines.
        cols: Initial width in characters.

    Returns:
        Whether it was started, was running already, or the image has no tmux.

    Raises:
        RuntimeError: tmux could not start the session.
    """
    code = _run(client, target, start_command(rows, cols))
    if code == 0:
        return SessionStart.STARTED
    if code == 1:
        return SessionStart.RUNNING
    if code == NO_TMUX:
        return SessionStart.NO_TMUX
    raise RuntimeError(f"tmux failed to start ({code})")


def session_running(client: Any, target: ShellTarget) -> bool:
    """Return whether the terminal's tmux session runs (blocking).

    Args:
        client: Docker client.
        target: Where and as whom the shell runs.

    Returns:
        True while the session runs; False when it doesn't or tmux is missing.
    """
    cmd = ["sh", "-c", f"tmux has-session -t {SESSION} 2>/dev/null"]
    return _run(client, target, cmd) == 0


def stop_session(client: Any, target: ShellTarget) -> None:
    """End the tmux session; its programs get SIGHUP (blocking).

    When a session was running, waits a moment so programs can exit before, for example, the
    container is removed.

    Args:
        client: Docker client.
        target: Where and as whom the shell runs.
    """
    script = f"tmux kill-session -t {SESSION} 2>/dev/null && sleep {STOP_GRACE}; true"
    _run(client, target, ["sh", "-c", script])


class Terminal:
    """One page's connection to the shell, through the Docker exec socket."""

    def __init__(self, client: Any, target: ShellTarget, *, tmux: bool = True) -> None:
        """Prepare a connection; ``open`` makes it.

        Args:
            client: Docker client (``docker.DockerClient``).
            target: Where and as whom the shell runs.
            tmux: Attach to the tmux session; False runs a plain shell instead.
        """
        self._client = client
        self._target = target
        self.tmux = tmux
        self._token = secrets.token_hex(16)
        self._exec_id = ""
        self._response: Any = None
        self._sock: socket.socket | None = None

    def open(self, rows: int = 24, cols: int = 80) -> None:
        """Connect to the shell (blocking; run it off the event loop).

        Args:
            rows: Terminal height in lines.
            cols: Terminal width in characters.
        """
        api = self._client.api
        kwargs = attach_kwargs(self._target, self._token, self.tmux)
        self._exec_id = api.exec_create(**kwargs)["Id"]
        self._response = api.exec_start(self._exec_id, tty=True, socket=True)
        self._sock = getattr(self._response, "_sock", self._response)
        self._sock.setblocking(False)
        self.resize(rows, cols)

    def resize(self, rows: int, cols: int) -> None:
        """Tell the TTY its new size (blocking).

        Args:
            rows: Terminal height in lines.
            cols: Terminal width in characters.
        """
        if self._exec_id and rows > 0 and cols > 0:
            with contextlib.suppress(Exception):  # the shell may just have exited
                self._client.api.exec_resize(self._exec_id, height=rows, width=cols)

    async def read(self) -> bytes:
        """Wait for output from the shell.

        Returns:
            The next bytes of output; empty when the shell has ended or the terminal is closed.
        """
        if self._sock is None:
            return b""
        try:
            return await asyncio.get_running_loop().sock_recv(self._sock, READ_SIZE)
        except OSError:
            return b""

    async def write(self, data: bytes) -> None:
        """Send typed input to the shell.

        Args:
            data: The input, as the terminal encodes it.
        """
        if self._sock is not None:
            with contextlib.suppress(OSError):
                await asyncio.get_running_loop().sock_sendall(self._sock, data)

    def close(self) -> None:
        """Disconnect; detaches from tmux, or ends a plain shell and its programs (blocking)."""
        sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()
        if self._response is not None:
            with contextlib.suppress(Exception):
                self._response.close()
            self._response = None
        if not self._exec_id:
            return
        try:
            _run(self._client, self._target, hangup_command(self._token))
        except Exception as exc:  # the container may be gone already
            log.debug("terminal hangup in %s: %s", self._target.container, exc)
