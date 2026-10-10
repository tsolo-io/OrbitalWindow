<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0004 One Docker network per user

- Status: accepted
- Date: 2026-10-09

## Context
In the POC all desktops shared one proxy network. A user with sudo in their own desktop could reach
another desktop's nginx, which was protected only by a password, and with `NET_RAW` could sniff plain
HTTP on that network.

## Decision
- Each desktop gets its own bridge network `ow-net-<user>` (with internet access), created at start
  and removed at stop.
- OrbitalWindow connects Traefik (and later sshpiper) to that network at start. The reconcile loop
  reconnects them if Traefik is recreated.
- Desktops run with `cap_drop: [NET_RAW]`. Their fixed MAC address is set on this network's endpoint.

## Consequences
- Desktops cannot reach each other at all, and nobody can reach a desktop except through Traefik.
- Traefik is attached to one network per running desktop. That is fine for a handful of users.
- The router label `traefik.docker.network` must name the user's network, or Traefik would pick
  the wrong one.