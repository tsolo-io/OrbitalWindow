<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# Preparing an AMD host

Do these steps before [Installing](01-install.md) on a server with AMD GPU(s).

## Driver

Install the `amdgpu` driver (and ROCm if users need GPU computing). Do **not** install the NVIDIA
Container Toolkit. Check that the GPU has a render node and that `/dev/kfd` exists:

```bash
ls -l /dev/dri/by-path /dev/kfd
```

## Setting

Set `OW_GPU_VENDOR=amd` in `.env` (or **Settings → GPU**). Then:

- Desktops get `/dev/dri` and `/dev/kfd` instead of an NVIDIA device request.
- **Video encoding** of the desktop stream uses VA-API instead of NVENC.
- **GPU numbers** on the dashboard and System page are hidden (they come from NVIDIA's NVML).
  The **Server status** page shows the GPU temperature (node-exporter) as *AMD GPU*.
- The **AI session** (vLLM) and model suggestions need NVIDIA.
- **MATLAB** GPU computing (`gpuArray`) needs NVIDIA; MATLAB itself runs fine.
- OrbitalWindow starts every desktop with `PIXELFLUX_WAYLAND=false` (needed on NVIDIA). On AMD the
  Wayland mode may also work; it is untested.
