<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0033 Terminal-only containers: LinuxServer base images in the desktop slot

- Status: accepted
- Date: 2026-10-16

## Context
Ticket #40 asks for containers without a desktop (GUI), for users who work in the terminal and
want the most performance. Admins define them with a "has a GUI" flag, and SSH is recommended.
Martin's answers to the scoping questions on the ticket:

- Use LinuxServer base images: Ubuntu and other distributions.
- Define them on the existing Desktops page. A page to pick LinuxServer options without typing
  may come later.
- One container per user at a time: a desktop or a terminal-only one.
- Switch SSH on automatically.
- Don't install add-ons that need a GUI.
- Use the same tuning and resources with and without a GUI.

## Decision
- **Images:**
  - `images.gui` (default true) and `images.base_image` (the full image to build on; NULL means
    `webtop:<base_tag>`, as before).
  - The admin picks *Terminal only* and types a LinuxServer image such as
    `baseimage-ubuntu:noble`. A name without a registry gets `lscr.io/linuxserver/`, and one
    without a tag gets `:latest`.
  - The image is built as `ow-terminal:<repo>-<tag>` from `deploy/image/Dockerfile.terminal`.
- **Why LinuxServer base images:** they bring s6, the `abc` user from `PUID`/`PGID`,
  `/config` as home and `/custom-cont-init.d`. So group folders, add-on builds, the per-user
  image, SSH (an s6 service) and the browser terminal (ADR 0030) work unchanged.
- **Dockerfile.terminal:**
  - adds the same command-line tools as the desktop image (tmux among them) and sudo;
  - gives `abc` bash as its login shell (the base images give it `/bin/false`, which blocks SSH);
  - the healthcheck waits for `/run/ow-ready`, which the init hook writes when it has finished.
- **Container:** the same name, network, mounts, groups, GPU, CPU/RAM limits and `shm_size` as a
  desktop. It has no Traefik labels and no desktop settings (`PASSWORD`, `SUBFOLDER`, …), Traefik
  is not connected to it, and `OW_GUI=false` makes the init hook skip the Desktop launchers.
- **Dashboard:** the Desktop list marks these *(terminal only)*. Without a desktop, **Open
  desktop** is hidden and a note points to **Open terminal** and SSH.
- **SSH:** choosing a terminal-only image switches the `ssh` add-on on when the user may use it,
  which queues the per-user build. A restricted, ungranted SSH stays off. The audit log records
  `addon.on ssh (no desktop)`.
- **GUI add-ons:** a new `gui: true` field in `addon.yaml` (VS Code, RStudio, MATLAB).
  Without a desktop such add-ons are greyed out ("needs a desktop") and left out of the image
  plan, even if they were switched on before. Switching back to a desktop brings them back.

## Consequences
- This step builds on apt-based images (Ubuntu, Debian), as the desktops do. Other distributions
  need the package manager to be abstracted; ADR 0034 does that.
- Users still have one container. Switching between a desktop and a terminal-only image works
  like switching desktops: stop, choose, start.
