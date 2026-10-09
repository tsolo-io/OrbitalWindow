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
- **cAdvisor v0.60.6** runs **unprivileged and without any Docker access**. Since Docker 29 uses
  containerd's image store, cAdvisor's Docker support needs the containerd socket, which is as
  powerful as the Docker socket. Instead cAdvisor reports plain cgroups
  (`--docker_only=false --raw_cgroup_prefix_whitelist=/system.slice/docker-,/docker/`). It mounts
  `/`, `/sys`, `/var/lib/docker` and `/dev/disk` read-only. Without `/dev/kmsg` it only loses OOM
  events, which we don't use.
- OrbitalWindow reads `/api/v2.0/summary/?type=name&recursive=true` (current CPU in millicores and
  memory; cached 5 s) and maps the container IDs in the cgroup paths to names with its own Docker
  client. Limits are shown from the database. If cAdvisor is down, the numbers are simply hidden.
- The cAdvisor UI is served at `/admin/cadvisor/` (`--url_base_prefix`, which also moves its API and
  `/healthz` there) behind the forwardAuth middleware `ow-auth-admin` → `/auth/admin`: admins get
  200, other users 403, no session → login. The same middleware will protect Dozzle. Its pages list
  cgroups by container ID, not name; the System page in OrbitalWindow shows names.
- GPU totals come from NVML in the service. On NVIDIA hosts `make start` adds
  `deploy/compose.nvidia.yaml`, which gives the service only the `utility` capability. Without a
  driver (AMD, no GPU) the GPU numbers are hidden.

## Consequences
- No privileged container and no Docker or containerd socket outside the proxies.
- CPU numbers are cAdvisor's latest sample (about 10 s old), which is fine for a dashboard.
