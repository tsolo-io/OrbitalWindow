# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

# OrbitalWindow desktop base image, built by the service as ow-desktop:<tag>.
# Keep it slim: optional software belongs in add-ons (addons/<key>/).
ARG BASE_IMAGE=lscr.io/linuxserver/webtop:ubuntu-xfce
FROM ${BASE_IMAGE}

# Ubuntu's nvtop carries cap_perfmon, which is outside Docker's bounding set, so exec
# fails; drop the capability (NVIDIA stats via NVML don't need it).
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      git python3 python3-venv curl unzip rsync nano htop nvtop libcap2-bin \
      mesa-utils vulkan-tools fonts-dejavu fonts-liberation fonts-noto-core \
 && (setcap -r /usr/bin/nvtop || true) \
 && rm -rf /var/lib/apt/lists/*

COPY root/ /

# Healthy once nginx answers (any HTTP status: basic auth gives 401). OrbitalWindow shows
# "Starting…" until then, so Open desktop never lands on a 502.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --start-interval=2s \
  CMD curl -s -o /dev/null http://127.0.0.1:3000/ || exit 1
