<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0030 A browser terminal that keeps running: xterm.js, docker exec and tmux

- Status: accepted
- Date: 2026-10-16

## Context
Ticket #39 asks for a terminal in the user's container on demand, through `docker exec`. The
ticket suggests ttyd and asks for other options. Martin added that the terminal should keep running
when the browser disconnects, so long science runs carry on. It should stop only on a button
press, and stopping the desktop should stop the terminal first.

Options considered:

- **ttyd (or wetty, gotty) in the desktop or in an extra container.** This is another web
  server per desktop. It needs its own Traefik router and forwardAuth route, and a port in every
  image. With ttyd in the service container, it would need `docker` CLI access as well.
- **SSH (already there through sshpiper).** This needs keys and a client on a Windows laptop, so
  it is the wrong tool for the users this ticket is for.
- **NiceGUI's `ui.xterm` (xterm.js) plus `docker exec` from the service.** No new process, port
  or route. The page uses the existing session and socket proxy, which already allows exec.

## Decision
- **Page `/terminal`:** `ui.xterm` connected to a TTY `docker exec` in `ow-<user>`, through
  `socket-proxy-rw`. The exec socket is read and written with asyncio. There is no user name in
  the URL: the page only opens the logged-in user's own desktop (ADR 0014). Logging out or
  losing the account leaves the page within 10 s.
- **Never root:** the shell runs as `abc` in `/config`, the same user as the desktop and SSH.
- **Kept running with tmux:** the terminal is a tmux session named `ow` inside the desktop.
  - A separate exec, which carries no page token, starts the session with `new-session -d`. The
    settings are mouse on (the wheel scrolls the history), a 50,000-line history,
    `window-size latest`, and then `~/.tmux.conf`.
  - The page attaches with `tmux attach-session`. Its exec carries `OW_TERMINAL=<random token>`.
- **Closing the page only detaches.** Docker keeps an exec running after its connection closes.
  When a page goes away, OrbitalWindow therefore sends SIGHUP to the processes whose environment
  holds that page's token: only the tmux client, which detaches.
- **Stopping:**
  - **Stop terminal** (with a confirmation) runs `tmux kill-session`. Its programs get SIGHUP.
  - Removing a running desktop (stop, admin stop, desktop switch, user removal) kills the session
    first and waits 2 s, so programs get SIGHUP rather than SIGKILL. The dashboard asks before
    stopping a desktop whose terminal runs.
- **Status:** the dashboard checks `tmux has-session` while the desktop runs. It shows *Your
  terminal is running* and a **Stop terminal** button.
- **tmux is in the desktop base image.** A desktop built before this change has no tmux. It gets
  a plain shell that ends with the page, and the page asks the user to have the desktop rebuilt.
- **Audit:** `terminal.start` (a new session) and `terminal.stop`.

## Consequences
- One terminal per desktop. Opening the page in two tabs shows the same session.
- Copying text needs Shift while selecting, because tmux owns the mouse. The help page says so.
- Output goes through the service's NiceGUI websocket. That is fine for interactive use, but a
  program printing megabytes per second is slower than over SSH.
- Admins still get no shell in users' desktops through the UI (ADR 0014).
- Non-desktop containers (ticket #40) can reuse this. They need tmux, or they get the plain
  shell.
