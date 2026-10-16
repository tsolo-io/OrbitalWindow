<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0032 Users' own environment variables, per user and applied at start

- Status: accepted
- Date: 2026-10-16

## Context
Ticket #41: users set environment variables for all their containers. The variables stay the same
when the user switches desktops. They are rarely needed, so the dashboard only links to a separate
page, where users list, add, change and remove them.

## Decision
- **Storage:** table `user_env (user_id, name, value)`, one row per variable, per user and not
  per desktop. Deleting a user removes the rows. At most 50 variables, 4,096 characters per value
  and no line breaks.
- **Names:** shell names (`[A-Za-z_][A-Za-z0-9_]*`, up to 64 characters). Refused are what the
  desktop spec sets (the add-on `RESERVED_ENV`), `PATH`, `HOME`, `USER`, `SHELL`, `HOSTNAME`,
  `LD_PRELOAD`, and the prefixes `OW_`, `S6_`, `LSIO_` and `NVIDIA_`. The container's start-up
  relies on these. The help page says how to extend `PATH` in `~/.bashrc` instead. `build_spec`
  skips reserved names again, in case the rules change after a variable was saved.
- **Applied at start:** the variables go into the container environment. They win over add-on
  `env`, because the user chose them explicitly.
  - Programs in the desktop and the browser terminal (`docker exec`) inherit the container
    environment.
  - sshd does not pass its environment on to logins. The container therefore also gets
    `OW_USER_ENV` (the names), and the init hook writes `/etc/profile.d/ow-user-env.sh` with
    quoted `export` lines for SSH logins. This needs the rebuilt desktop base image.
- **Restart note:** the desktop's `ow.env` label holds a hash of the variables it was started
  with. The page shows "Stop and start your desktop" when the saved variables differ.
- **Privacy:** the values are shown only to the user on `/environment`. The audit log records
  the names, never the values, because values may be tokens. Admin pages don't show them
  (ADR 0014).

## Consequences
- Changes need a desktop restart, as SSH keys and add-on mounts do.
- The values are visible in `docker inspect` on the host, like everything else in the container
  spec. Admins with host access can read them; the help page treats them like a password.
- Non-desktop containers (ticket #40) get them through the same spec.
