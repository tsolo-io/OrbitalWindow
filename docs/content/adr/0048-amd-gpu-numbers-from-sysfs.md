<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0048 AMD GPU numbers: read from the amdgpu driver's sysfs files

- Status: accepted
- Date: 2026-11-16

## Context
OrbitalWindow shows GPU load, memory and temperature on the dashboard, System, Server status and
AI pages. The AI page also uses the GPU memory for the fit check and the model suggestions, and
an AI session does not start without GPU numbers. NVIDIA hosts read NVML (ADR 0017); Intel hosts
read the `ow-gpu-stats` exporter (ADR 0036). AMD hosts had no source, so they showed no GPU,
and the AI session could not start there. The first AMD production server (RX 7900 XTX next to the
graphics built into a Ryzen 9 9950X) showed exactly that.

The amdgpu driver writes the numbers to sysfs files that any user can read:
`gpu_busy_percent`, `mem_info_vram_used` and `mem_info_vram_total` in the card's `device` folder,
and the temperatures in its hwmon folder. sysfs is mounted read-only in every container and shows
the host's devices, so the service can read them as its non-root user.

## Decision
- `AmdGpus` in `metrics.py` reads those files for every DRM card with vendor `0x1002`.
  `GpuMetrics` uses it while the **GPU** setting is `amd` (the setting can change while the
  service runs). No exporter, no capability, no compose file.
- **A sleeping GPU** (runtime power management, the normal state of an idle discrete GPU)
  counts as idle with no temperature: amdgpu then refuses to read its load and temperature
  ("Device or resource busy") rather than wake it, while its memory still reads. OrbitalWindow
  does not wake it.
- **Temperature** is the `edge` sensor, the one other tools show; the slowdown limit is its
  `crit` value, where amdgpu throttles.
- **Built-in graphics** (APUs) are left out when there is a discrete GPU: they share system memory
  and desktops render on the discrete GPU. amdgpu gives APUs neither `mem_info_vram_vendor` nor a
  critical temperature; a card with neither counts as built-in. A host with only built-in
  graphics shows them.
- **Index** is the HIP device number: the GPU's place among the GPU nodes of the KFD topology
  (`/sys/class/kfd/kfd/topology/nodes`, matched by PCI address). That is the number the AI
  session puts in `HIP_VISIBLE_DEVICES`. Without the topology, the PCI order is used.
- **Name** comes from libdrm's `amdgpu.ids` (by PCI device and revision), the table Mesa uses.
  The service image installs `libdrm-common` for it. A card not in the table is called
  `AMD GPU 0x<device>`.

## Consequences
- AMD hosts show the same GPU numbers as NVIDIA and Intel hosts, the fit check and suggestions
  work, and the AI session (llama.cpp with ROCm) can start.
- The service image gets one small Debian package. A GPU newer than the image's libdrm shows its
  PCI device ID instead of its name until the image is rebuilt.
- `gpu_busy_percent` is the driver's own load figure, sampled when read; it is not averaged.
- The built-in graphics' temperature is no longer on the Server status page once the GPU numbers
  have a temperature (ADR 0036 shows a GPU's temperature once, and node-exporter labels both
  chips *AMD GPU*).
