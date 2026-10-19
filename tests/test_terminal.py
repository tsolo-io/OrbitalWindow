# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The browser terminal: the tmux session, attaching and hanging up, and desktop stops."""

import subprocess
from types import SimpleNamespace

import pytest

from orbitalwindow import terminal
from orbitalwindow.desktops import DesktopError, DesktopState, desktop_shell
from orbitalwindow.terminal import (
    NO_TMUX,
    ShellTarget,
    Terminal,
    attach_kwargs,
    hangup_command,
    start_command,
)

TARGET = ShellTarget("ow-user1", "abc", "/config")


def test_desktop_shell_is_abc_in_home():
    """A desktop's terminal runs as abc (never root) in /config."""
    assert desktop_shell("user1") == TARGET


def test_attach_runs_as_the_desktop_user_with_a_tty():
    """Attaching runs tmux as abc in the home folder, with a TTY and the page's token."""
    kwargs = attach_kwargs(TARGET, "f00d", tmux=True)
    assert kwargs["cmd"] == ["tmux", "attach-session", "-t", "ow"]
    assert kwargs["user"] == "abc" and kwargs["workdir"] == "/config"
    assert kwargs["tty"] and kwargs["stdin"]
    assert kwargs["environment"] == {
        "HOME": "/config",
        "TERM": "xterm-256color",
        "OW_TERMINAL": "f00d",
    }
    assert attach_kwargs(TARGET, "f00d", tmux=False)["cmd"][:2] == ["sh", "-c"]


def test_start_command_is_valid_shell_without_token():
    """The session start is valid shell and doesn't carry a page token (the server outlives it)."""
    cmd = start_command(30, 100)
    assert cmd[:2] == ["sh", "-c"] and "OW_TERMINAL" not in cmd[2]
    assert "-x 100 -y 30" in cmd[2] and f"exit {NO_TMUX}" in cmd[2]
    subprocess.run(["sh", "-n", "-c", cmd[2]], check=True)


def test_hangup_kills_only_the_page_processes():
    """SIGHUP reaches processes carrying this page's token and no others."""
    env = {"PATH": "/usr/bin:/bin"}
    mine = subprocess.Popen(["sleep", "60"], env=env | {"OW_TERMINAL": "aa11"})
    other = subprocess.Popen(["sleep", "60"], env=env | {"OW_TERMINAL": "bb22"})
    try:
        subprocess.run(hangup_command("aa11"), check=True)
        assert mine.wait(timeout=5) == -1  # SIGHUP
        assert other.poll() is None
    finally:
        other.kill()
        mine.kill()


@pytest.mark.parametrize(
    ("code", "result"),
    [(0, "started"), (1, "running"), (NO_TMUX, "no-tmux")],
)
def test_ensure_session_reports_what_happened(service, code, result):
    """The start command's exit code tells started, already running and no tmux apart."""
    container = service.client.containers.get.return_value
    container.exec_run.return_value = SimpleNamespace(exit_code=code, output=b"")
    assert terminal.ensure_session(service.client, TARGET, 24, 80) == result
    assert container.exec_run.call_args.kwargs["user"] == "abc"


def test_close_hangs_up_in_the_container(service):
    """Closing a page's connection runs the hang-up as the shell's user in its container."""
    term = Terminal(service.client, TARGET)
    service.client.api.exec_create.return_value = {"Id": "exec1"}
    term.open(30, 100)
    service.client.api.exec_resize.assert_called_with("exec1", height=30, width=100)
    term.close()
    container = service.client.containers.get.return_value
    assert container.exec_run.call_args.args[0] == hangup_command(term._token)


def test_open_terminal_needs_a_running_desktop(service, users):
    """No terminal while the desktop is stopped."""
    service.desktops.state = lambda name: DesktopState.STOPPED
    with pytest.raises(DesktopError, match="not running"):
        service.open_terminal("user1", users["user1"]["id"], 24, 80)
    assert not service.terminal_running(users["user1"]["id"])


def test_open_terminal_starts_once_in_the_own_desktop(service, users, monkeypatch):
    """The first open starts the session in ow-<user> (audited); later ones attach to it."""
    service.desktops.state = lambda name: DesktopState.RUNNING
    results = iter([terminal.SessionStart.STARTED, terminal.SessionStart.RUNNING])
    monkeypatch.setattr(terminal, "ensure_session", lambda *args: next(results))
    monkeypatch.setattr(terminal.Terminal, "open", lambda self, rows, cols: None)
    first = service.open_terminal("user1", users["user1"]["id"], 24, 80)
    second = service.open_terminal("user1", users["user1"]["id"], 24, 80)
    assert first._target == TARGET and first.tmux and second.tmux
    assert [e["action"] for e in service.audit_log("terminal.")] == ["terminal.start"]


def test_open_terminal_without_tmux_is_a_plain_shell(service, users, monkeypatch):
    """A desktop image without tmux gets a plain shell."""
    service.desktops.state = lambda name: DesktopState.RUNNING
    monkeypatch.setattr(terminal, "ensure_session", lambda *args: terminal.SessionStart.NO_TMUX)
    monkeypatch.setattr(terminal.Terminal, "open", lambda self, rows, cols: None)
    assert not service.open_terminal("user1", users["user1"]["id"], 24, 80).tmux


def test_stopping_the_desktop_stops_the_terminal_first(service, users, monkeypatch):
    """Stopping a running desktop ends its terminal before the container is removed."""
    events = []
    container = service.client.containers.get.return_value
    container.status = "running"
    container.remove.side_effect = lambda force: events.append("remove")
    monkeypatch.setattr(
        "orbitalwindow.desktops.stop_session",
        lambda client, target: events.append(("stop terminal", target)),
    )
    service.stop_desktop("user1", users["user1"]["id"])
    assert events == [("stop terminal", TARGET), "remove"]
