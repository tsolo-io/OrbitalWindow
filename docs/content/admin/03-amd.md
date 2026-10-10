<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# AMD hosts

OrbitalWindow also runs on servers with AMD GPUs, with these differences:

- **Host:** install the `amdgpu` driver (and ROCm if users need GPU computing). Do **not** install
  the NVIDIA Container Toolkit.
- **Setting:** `OW_GPU_VENDOR=amd` in `.env` (or **Settings → GPU**). Desktops then get
  `/dev/dri` and `/dev/kfd` instead of an NVIDIA device request.
- **Video encoding** of the desktop stream uses VA-API instead of NVENC.
- **GPU numbers** on the dashboard and System page are hidden (they come from NVIDIA's NVML).
- **MATLAB** GPU computing (`gpuArray`) needs NVIDIA; MATLAB itself runs fine.
- OrbitalWindow starts every desktop with `PIXELFLUX_WAYLAND=false` (needed on NVIDIA). On AMD the
  Wayland mode may also work; it is untested.

For a test machine without a GPU, use `OW_GPU_VENDOR=none`: desktops render in software.
