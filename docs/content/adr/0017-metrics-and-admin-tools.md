<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0017 Metrics from cAdvisor and NVML; admin tools behind forwardAuth

- Status: accepted
- Date: 2026-10-09

## Context
Users should see their own CPU/RAM and the shared GPU load; admins everything, plus the cAdvisor UI.
The design pins cAdvisor v0.60.x and reads GPU totals with NVML. cAdvisor is usually run privileged
with `/var/run` mounted, which would expose the Docker socket, against
[0005](0005-docker-access-through-socket-proxies.md).

## Decision
- **cAdvisor v0.60.6** runs **unprivileged** and talks to Docker through the **read-only socket
  proxy** (`--docker=tcp://socket-proxy-ro:2375`; the read-only proxy now also allows `/info`).
  It mounts `/`, `/sys`, `/var/lib/docker` and `/dev/disk` read-only. Without `/dev/kmsg` it only
  loses OOM events, which we don't use.
- It is served at `/admin/cadvisor/` (`--url_base_prefix`, which also moves its API there) behind
  the forwardAuth middleware `ow-auth-admin` → `/auth/admin`: admins get 200, other users 403, no
  session → login. The same middleware will protect Dozzle.
- OrbitalWindow reads `/api/v2.0/spec` (names, labels; cached 60 s) and `/api/v2.0/summary`
  (current CPU in millicores and memory; cached 5 s) and keeps only containers labelled
  `ow.managed=true`. If cAdvisor is down, the numbers are simply hidden.
- GPU totals come from NVML in the service. On NVIDIA hosts `make start` adds
  `deploy/compose.nvidia.yaml`, which gives the service only the `utility` capability. Without a
  driver (AMD, no GPU) the GPU numbers are hidden.

## Consequences
- No extra privileged container and no new raw socket mount.
- CPU numbers are cAdvisor's latest sample (about 10 s old), which is fine for a dashboard.
