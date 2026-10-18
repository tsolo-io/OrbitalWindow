<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0035 Intel GPUs: `gpu_vendor=intel` passes only `/dev/dri`

- Status: accepted
- Date: 2026-10-18

## Context
The server *Wired* has an Intel Arc A770. OrbitalWindow must work with NVIDIA, AMD and Intel GPUs
(ticket #50). ADR 0011 has only `nvidia`, `amd` and `none`.

A test on Wired with the existing desktop image (`ubuntu-xfce`) and only `/dev/dri` passed in gave:

- `glxinfo -B`: `OpenGL renderer string: Mesa Intel(R) Arc(tm) A770 Graphics (DG2)`, accelerated.
- `vulkaninfo`: the Intel Mesa driver (ANV).
- Selkies: `Render node 0 encodes H264, VP9, AV1, H265 on vaapi.`

The image already contains Mesa and `intel-media-va-driver-non-free` (iHD). LinuxServer's init adds
`abc` to the groups of the `/dev/dri` nodes.

## Decision
- Add `intel` to the GPU vendor setting. Desktops get `/dev/dri`, with no device request and no
  `/dev/kfd`. No change to the desktop image.
- Name the `i915` and `xe` hwmon chips *Intel GPU* on the Server status page.
- GPU load and memory stay NVIDIA only (NVML). The AI session (vLLM) and model suggestions stay
  NVIDIA only.
- The admin guide has one preparation page per vendor under *Installing*.

## Consequences
- Intel desktops are GPU-accelerated and stream with VA-API, with no new dependency.
- The dashboard and System page show no Intel GPU numbers. Intel load could come from the i915 PMU
  (as `intel_gpu_top` reads it); this is not done.
- GPU computing (OpenCL, oneAPI) in desktops needs Intel's compute runtime in the image. Not done.
- vLLM has an Intel (XPU) build. Using it is a separate decision.
