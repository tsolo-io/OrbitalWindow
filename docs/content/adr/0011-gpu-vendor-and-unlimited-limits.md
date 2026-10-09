<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0011 GPU vendor setting and "unlimited" CPU/RAM

- Status: accepted
- Date: 2026-10-09

## Context
Most production hosts have NVIDIA GPUs, some AMD. Development and CI machines may have none. Admins
want to set per-user CPU/RAM limits, or no limit.

## Decision
- The runtime setting `gpu_vendor` is `nvidia`, `amd` or `none`.
  - `nvidia`: `/dev/dri` plus a device request for all GPUs, and `NVIDIA_DRIVER_CAPABILITIES=all`.
  - `amd`: `/dev/dri` and `/dev/kfd`.
  - `none`: no GPU devices. The desktop renders in software. This is for testing only.
- `users.cpus` / `users.mem_gb` (and the defaults in `settings`) are NULL for **unlimited**. 0 and
  empty input also mean unlimited.

## Consequences
- The stack and its tests run on machines without a GPU.
- GPU checks (NVENC, zink, NVML) can only be verified on a GPU host.