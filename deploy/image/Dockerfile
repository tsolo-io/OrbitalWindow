# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

# OrbitalWindow desktop base image, built by the service as ow-desktop:<tag>.
# Keep it slim: optional software belongs in add-ons (addons/<key>/).
ARG BASE_IMAGE=lscr.io/linuxserver/webtop:ubuntu-xfce
FROM ${BASE_IMAGE}

# Ubuntu's nvtop carries cap_perfmon, which is outside Docker's bounding set, so exec
# fails; drop the capability (NVIDIA stats via NVML don't need it).
# tmux keeps the browser terminal running without a browser (ADR 0030).
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      git python3 python3-venv curl unzip rsync nano htop nvtop tmux libcap2-bin \
      mesa-utils vulkan-tools fonts-dejavu fonts-liberation fonts-noto-core \
 && (setcap -r /usr/bin/nvtop || true) \
 && rm -rf /var/lib/apt/lists/*

COPY root/ /

# Healthy once Selkies (the stream server, port 8082) answers, which is up to a minute after
# nginx. OrbitalWindow shows "Starting…" and Traefik does not route to the desktop until then, so
# a browser never connects to a half-started stream server (its page then stays broken).
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --start-interval=2s \
  CMD curl -fs -o /dev/null "http://127.0.0.1:8082${SUBFOLDER:-/}api/status" || exit 1
