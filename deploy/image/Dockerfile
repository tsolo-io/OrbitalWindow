# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

# OrbitalWindow desktop base image, built by the service as ow-desktop:<tag>.
# Keep it slim: optional software belongs in add-ons (addons/<key>/). Webtop tags of any
# distribution with apt, apk, dnf or pacman work (ubuntu-*, debian-*, fedora-*, alpine-*, arch-*).
ARG BASE_IMAGE=lscr.io/linuxserver/webtop:ubuntu-xfce
FROM ${BASE_IMAGE}

# The base tools for any distribution ow-pkg supports (ADR 0034); tmux keeps the browser
# terminal running without a browser (ADR 0030).
COPY root/usr/local/bin/ow-pkg /usr/local/bin/ow-pkg
COPY base-packages.sh /tmp/base-packages.sh
# The host's GPU vendor (the GPU setting): Intel images get the compute runtime.
ARG GPU_VENDOR=none
RUN sh /tmp/base-packages.sh desktop "$GPU_VENDOR" && rm /tmp/base-packages.sh

COPY root/ /

# Healthy once Selkies (the stream server, port 8082) answers, which is up to a minute after
# nginx. OrbitalWindow shows "Starting…" and Traefik does not route to the desktop until then, so
# a browser never connects to a half-started stream server (its page then stays broken).
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --start-interval=2s \
  CMD curl -fs -o /dev/null "http://127.0.0.1:8082${SUBFOLDER:-/}api/status" || exit 1
