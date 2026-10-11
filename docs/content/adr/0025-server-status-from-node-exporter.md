<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0025 Server status from node-exporter, read only while the page is open

- Status: accepted
- Date: 2026-10-10

## Context
Ticket #22: a page where every user sees how busy the server is: memory, CPU, disk space and I/O,
load. The server runs Prometheus node-exporter on its default port; OrbitalWindow doesn't start
it. The page must not cache or preload: nothing is read unless someone has the page open.

## Decision
- **Source:** the service reads node-exporter's `/metrics` over HTTP
  (`OW_NODE_EXPORTER_URL`, default `http://host.docker.internal:9100/metrics`). The compose file
  maps `host.docker.internal` to the host with `extra_hosts: host-gateway`. A small parser
  (`hoststats.py`) reads the text format; no Prometheus server is involved.
- **Read by the page itself:**
  - Each open `/status` page reads once on open and then every 5 seconds through its own timer,
    which stops when the page is closed.
  - Rates (CPU use, disk and network bytes per second, disk busy time) come from the difference to
    that page's previous read, so they appear from the second read on.
- **What is shown:**
  - host name, OS, uptime, load (1, 5 and 15 minutes) with a quiet / busy / overloaded word
    relative to the number of CPUs;
  - CPU, memory and swap use, and disk space per real filesystem (one per device; tmpfs,
    overlay (Docker's container layers) and `/boot` are left out);
  - traffic per whole disk and per physical network interface (partitions, loop devices, `lo`,
    `veth*`, `br-*` and `docker*` are left out);
  - GPU load and memory from NVML, which the service already reads for the admin page.
- **Without node-exporter**, the page says that an administrator needs to install it. Admins also
  see the URL and the error.
- **Access:** every logged-in user, linked as **Server status** in the header.

## Consequences
- Each open status page makes one small HTTP request every 5 seconds. With a handful of users that
  is negligible, and when nobody is looking, nothing runs.
- node-exporter must be reachable from Docker's networks on port 9100, so a host firewall needs a
  rule. It is documented in the install guide.
