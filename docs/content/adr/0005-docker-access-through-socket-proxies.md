<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0005 Docker access only through socket proxies; the service is not root

- Status: accepted
- Date: 2026-10-09

## Context
Access to the Docker API is root-equivalent on the host. OrbitalWindow needs it to create desktops
and run builds. Traefik needs read access to discover routers.

## Decision
- Only the two `tecnativa/docker-socket-proxy` containers mount `/var/run/docker.sock`.
  - `socket-proxy-ro` (Traefik): containers and events, GET only.
  - `socket-proxy-rw` (OrbitalWindow): containers, exec, images, build and networks. Volumes, swarm,
    secrets, plugins, system and info stay off.
- Each proxy sits on its own `internal` network, reachable only by its one client.
- The OrbitalWindow container runs as UID 10001 with a read-only root filesystem, all capabilities
  dropped and `no-new-privileges`.
- File work that needs root under `$OW_ROOT` (creating a home folder owned by the user's UID,
  removing one, preparing the folder layout) runs in a short-lived `alpine` helper container
  (`hostops.py`): no network, only `CHOWN`/`DAC_OVERRIDE`/`FOWNER`, and user values passed as argv,
  never through a shell string.

## Consequences
- OrbitalWindow is still effectively root through the API (it could start a privileged container).
  The proxy limits the attack surface; the rest is argon2 hashes, a login rate limit, an audit log and
  using no Docker features beyond what the design needs.
- A helper run costs about a second. That is acceptable for rare admin actions.