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

Switch on **Resizable BAR** (also called *Re-Size BAR* or *Smart Access Memory*) and *Above 4G
decoding* in the BIOS. Arc GPUs are much slower without it, and the compute runtime warns
`Resizable BAR not detected` when it is off.

If the server also has a GPU in the CPU (integrated graphics), switch it off in the BIOS. If not,
desktops can use the integrated GPU instead of the Arc card. Servers with two GPUs are untested.

## Setting

Set `OW_GPU_VENDOR=intel` in `.env` (or **Settings → GPU**). Then:

- Desktops get `/dev/dri` only (no device request, no `/dev/kfd`).
- The desktop stream uses **VA-API** for video encoding. The desktop log shows
  `Render node 0 encodes H264, VP9, AV1, H265 on vaapi`.
- OpenGL and Vulkan in the desktop run on the GPU: `glxinfo -B` shows
  `OpenGL renderer string: Mesa Intel(R) Arc(tm) A770 Graphics (DG2)`.
- `make start` adds `compose.intel.yaml`. It starts `ow-gpu-stats`, a small exporter that reads the
  GPU's load, memory and temperature (for a plain `docker compose`, add it to `COMPOSE_FILE`). The
  dashboard, System, Server status and AI pages then show the GPU numbers, and the AI page can
  check if a model fits and suggest models. The exporter runs as root with only the
  `CAP_PERFMON` capability, which the kernel requires for these numbers.
- **GPU load** is the busiest engine (render, compute, copy or video) in the last 2 seconds.
- The **AI session** runs vLLM's Intel image (`OW_VLLM_INTEL_IMAGE`, about 17 GB). On the Arc
  A-series and Flex GPUs (tested: A770) OrbitalWindow starts it with `--dtype half` (vLLM refuses
  bfloat16 on the A770), `--attention-backend TRITON_ATTN` (vLLM's Flash Attention kernels need
  the newer Xe2 GPUs) and a context cache of at most 3.75 GB (these GPUs can't address larger
  memory blocks). So the longest context is shorter than on an NVIDIA GPU with the same memory:
  about 35,000 tokens for Qwen3-0.6B and 27,000 for Qwen3-8B. Newer Intel GPUs (Arc B-series)
  run vLLM's defaults; they are untested.
  **Known problem:** on the A770 a context over about 5,100 tokens hangs the GPU (ADR 0038), so
  the chat does not work yet.
- **GPU computing:** desktop and terminal-only images built on an Intel host get Intel's compute
  runtime: OpenCL (`intel-opencl-icd`), oneAPI Level Zero (`libze-intel-gpu1`) and `clinfo`.
  `clinfo -l` in a desktop lists the GPU. Programs such as PyTorch for Intel GPUs (XPU) use it.
  When you change the GPU setting, OrbitalWindow rebuilds every desktop image, and users get the
  new image the next time they start their desktop. **MATLAB** GPU computing (`gpuArray`) needs
  NVIDIA; MATLAB itself runs fine.
- OrbitalWindow starts every desktop with `PIXELFLUX_WAYLAND=false`. On Intel the Wayland mode may
  also work; it is untested.
