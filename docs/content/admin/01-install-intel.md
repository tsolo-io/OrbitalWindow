<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# Preparing an Intel host

Do these steps before [Installing](01-install.md) on a server with an Intel GPU (Arc). Tested with
an **Arc A770** on Ubuntu 24.04 (kernel 6.17, `i915` driver).

## Driver

The desktop image contains the user-space drivers: Mesa (OpenGL `iris`, Vulkan `ANV`) and the
VA-API driver `iHD` for video encoding. The host needs only the kernel driver. Do **not** install
the NVIDIA Container Toolkit.

1. Use a recent kernel. Ubuntu 24.04's hardware enablement (HWE) kernel supports Arc A-series
   (`i915`) and the newer B-series (`xe`):

    ```bash
    sudo apt install linux-generic-hwe-24.04 && sudo reboot
    ```

2. Check that the GPU has a render node and a kernel driver:

    ```bash
    ls -l /dev/dri/by-path                                  # a pci-…-render link to renderD128
    basename "$(readlink /sys/class/drm/renderD128/device/driver)"   # i915 or xe
    ```

3. Optional: `sudo apt install intel-gpu-tools`, then `sudo intel_gpu_top` shows the GPU load on
   the host.

If the server also has a GPU in the CPU (integrated graphics), switch it off in the BIOS. If not,
desktops can use the integrated GPU instead of the Arc card. Servers with two GPUs are untested.

## Setting

Set `OW_GPU_VENDOR=intel` in `.env` (or **Settings → GPU**). Then:

- Desktops get `/dev/dri` only (no device request, no `/dev/kfd`).
- The desktop stream uses **VA-API** for video encoding. The desktop log shows
  `Render node 0 encodes H264, VP9, AV1, H265 on vaapi`.
- OpenGL and Vulkan in the desktop run on the GPU: `glxinfo -B` shows
  `OpenGL renderer string: Mesa Intel(R) Arc(tm) A770 Graphics (DG2)`.
- **GPU numbers** (load, memory) on the dashboard and System page are hidden: they come from
  NVIDIA's NVML. The **Server status** page shows the GPU temperature (node-exporter) as
  *Intel GPU*.
- The **AI session** (vLLM) and model suggestions need NVIDIA.
- **GPU computing** (OpenCL, oneAPI) is not in the desktop image. **MATLAB** GPU computing
  (`gpuArray`) needs NVIDIA; MATLAB itself runs fine.
- OrbitalWindow starts every desktop with `PIXELFLUX_WAYLAND=false`. On Intel the Wayland mode may
  also work; it is untested.
