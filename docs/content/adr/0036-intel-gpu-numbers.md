<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0036 Intel GPU numbers: an exporter with only `CAP_PERFMON`

- Status: accepted
- Date: 2026-10-18

## Context
OrbitalWindow shows GPU load, memory and temperature on the dashboard, System, Server status and
AI pages. The AI page also uses the GPU memory for the fit check and the model suggestions. On
NVIDIA these come from NVML. Intel GPUs must be fully supported (ticket #50).

The i915 driver gives the numbers, but with conditions (tested on an Arc A770):

- **Load:** the i915 PMU counts busy time per engine. `perf_event_open` on it needs
  `CAP_PERFMON`, because Ubuntu sets `perf_event_paranoid` to 4.
- **Memory:** the `DRM_I915_QUERY_MEMORY_REGIONS` query gives the size of the GPU memory. It gives
  the real free memory only to a caller with `CAP_PERFMON`; for others, free equals the size.
- **Temperature:** hwmon in sysfs, which anyone can read.

A non-root process in a container can't use an added capability, and the service runs as non-root
(ADR 0005).

## Decision
- `deploy/scripts/intel-gpu-stats.py` (Python standard library only) reads the numbers and
  answers `GET /` with JSON in the fields of `GpuStat`. Load is the busiest engine over the last
  2 seconds.
- `compose.intel.yaml` runs it as `ow-gpu-stats` in `python:3.14-slim`: root, `cap_drop: ALL`,
  `cap_add: PERFMON`, `/dev/dri`, read-only, `no-new-privileges`, on its own internal network
  `ow-gpu`. It sets `OW_GPU_STATS_URL` for the service, and `GpuMetrics` then reads the exporter
  instead of NVML. `make start` adds the file when `OW_GPU_VENDOR=intel`.
- The script is in `deploy/scripts/` and runs in a public image, so servers need only the
  `deploy/` folder (ADR 0023).
- The Server status page shows a GPU's temperature once: from the GPU numbers if they have it,
  else from node-exporter.

## Consequences
- Intel hosts show the same GPU numbers as NVIDIA hosts, and the fit check and suggestions work.
- One more container with a capability. `CAP_PERFMON` lets it read performance counters; it has no
  other capability and no access to the Docker socket or to user data.
- Only the `i915` driver is supported. GPUs on the newer `xe` driver (Arc B-series) have other
  queries and PMU events; they show no numbers until the exporter supports `xe`.
