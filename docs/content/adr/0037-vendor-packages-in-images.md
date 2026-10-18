<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0037 GPU vendor packages in images, and GPU device groups for `abc`

- Status: accepted
- Date: 2026-10-18

## Context
On NVIDIA hosts, the NVIDIA Container Toolkit adds the driver's CUDA and OpenCL libraries to every
container that gets the GPU, so desktops can compute on the GPU without an install. Intel has no
such toolkit: the user-space compute runtime (OpenCL, oneAPI Level Zero) must be in the image.
Users of Intel hosts must be able to compute on the GPU too (ticket #50).

A test on Wired (Arc A770) also showed that in a terminal-only image (ADR 0033) `abc` can't open
`/dev/dri/renderD128`: the node belongs to the host's `render` group (GID 992), and LinuxServer's
plain base images, unlike webtop, don't add `abc` to the groups of the GPU nodes. This affects
Intel and AMD; NVIDIA's device nodes are open to all users.

## Decision
- The base image build gets the build argument `GPU_VENDOR` from the GPU setting.
  `base-packages.sh` installs the vendor's packages as extras (skipped where a distribution lacks
  them). Intel: `intel-opencl-icd libze-intel-gpu1 libze1 clinfo` on apt, and the matching
  packages on dnf, pacman and apk.
- Saving a different GPU setting queues a rebuild of every desktop and terminal-only image. Users'
  images follow their base image, as before.
- The init hook `50-ow-setup` adds `abc` to the group of every `/dev/dri/*` node and `/dev/kfd`,
  and creates a group (`ow-gpu<GID>`) for a GID the image doesn't know. It skips GID 0.

## Consequences
- On Intel, OpenCL and Level Zero work in desktops and terminal-only containers (tested with
  `clinfo` and `zeInit`/`zeDeviceGet` as `abc`). PyTorch XPU and other oneAPI programs can use them.
- Images on NVIDIA and AMD hosts don't carry the Intel packages.
- A vendor change costs a rebuild of every image (10–20 minutes each).
- AMD terminal-only containers can now open the GPU too. AMD's own compute (ROCm) is not added to
  the images.
