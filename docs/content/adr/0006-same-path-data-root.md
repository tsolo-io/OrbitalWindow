<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0006 `$OW_ROOT` is mounted at the same path in the service

- Status: accepted
- Date: 2026-10-09

## Context
OrbitalWindow runs in a container but creates other containers whose bind mounts are host paths
(homes, scratch, shared software). It also reads and writes files there (DuckDB, `users.json`).

## Decision
Mount the host's `$OW_ROOT` (default `/srv/orbitalwindow`) into the service at the **same path**,
and pass the same value in `OW_ROOT`.

## Consequences
- One path works both for the service's own file access and as a mount source for Docker.
  No host/container path translation code.
- Moving the data root means changing `OW_ROOT` in `deploy/.env` and moving the folder.