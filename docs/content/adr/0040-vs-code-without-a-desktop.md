<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0040 VS Code without a desktop: code-server per user, on demand

- Status: accepted
- Date: 2026-10-18

## Context
Users want to edit the files in their home folder with VS Code without starting their desktop
(#49). The ticket asks for LinuxServer's code-server image, in its own container.

LinuxServer's image keeps code-server's settings and extensions in `/config/data` and
`/config/extensions` and creates `/config/workspace`. At start it changes the owner of everything in
`/config` when `.profile` has another owner. Mounted over a user's home, that would add three
visible folders and could change the owner of files.

## Decision
- A container `ow-code-<user>` per user, started and stopped on the dashboard (VS Code uses a few
  hundred MB of RAM, so it does not run all the time like the file manager). It keeps running until
  stopped, also over restarts.
- Image `ow-code:<hash>`: built by the service from `deploy/image/Dockerfile.code` on
  `OW_CODE_SERVER_IMAGE` (default `lscr.io/linuxserver/code-server:4.140.0-ls368`), with:
  - its own init and service scripts: settings and extensions in `~/.local/share/code-server` of the
    home (`/home`), no
    folders created and no owner changed in the home, workspace trust off on the first start (it is
    the user's own home);
  - Python 3 (for VS Code's Python support);
  - OrbitalWindow's init hook (groups, GPU groups, the user's environment variables).
  The tag is a hash of these files and the base image, so a change rebuilds it on the next start.
- The same folders as the desktop: `/groups/<name>`, `/scratch` and `/shared` (read-only) at the
  same paths, but the home at `/home` (`HOME=/home`, and `abc`'s home): users look for their files
  there, and LinuxServer's `/config` is a puzzling name in an editor (changed 2026-10-21; the
  desktop keeps `/config`). Runs as the user's UID (`PUID`), with the user's CPU and RAM limits.
- Access like the file manager: `/code/<user>/`, owner-only forwardAuth (`/auth/code`), the prefix
  stripped for code-server; no password in code-server, and its network `ow-cnet-<user>` is joined
  only by Traefik. `no-new-privileges`, so no sudo.

## Consequences
- Users edit their files without a desktop; changes are at once in the desktop, the file manager
  and Samba.
- The VS Code terminal has Python 3 and Git, not the user's desktop software. Running heavy work
  stays in the desktop or terminal-only container.
- Extensions come from Open VSX (code-server can't use Microsoft's marketplace).
- One more image to build (about 1 minute the first time) and update (`OW_CODE_SERVER_IMAGE`).
