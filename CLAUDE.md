<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# OrbitalWindow

A Python **FastAPI + NiceGUI** service for managing GPU-accelerated research/science desktop
environments (LinuxServer *webtop* containers) on Docker. Users log in to OrbitalWindow, then
start, stop and open their own browser desktop. Admins manage users, desktop images, add-ons and groups.

## Read first
- `docs/content/design.md`: the agreed design. The **source of truth** for what to build, including the phases and the verification checklist.
- `docs/content/poc-learnings.md`: what a hand-built proof of concept verified and the problems it hit. Read it before
  touching desktop images, GPUs, Traefik, Samba or MATLAB. Most "why is it like this" answers are there.

## Status
Nothing is implemented yet. Start with **phase 1** in `docs/content/design.md`. The POC is not carried over: no
migration, no backward compatibility, no redirects. Build clean from the design.

## Project conventions
- **Build:** hatch / hatchling, src layout (`src/orbitalwindow/`), Python ≥ 3.14.
- **Hatch environments:**
  - `make test`: pytest (built-in `hatch-test` env).
  - `make format` or `hatch run lint:check` / `hatch run lint:fmt`: ruff.
  - `hatch run docs:build` / `hatch run docs:serve`: zensical, run as `zensical build -f docs/zensical.toml`.
    The config is empty for now. It needs at least `site_name` and a docs directory/nav, so set it up in phase 1.
- **Database:** DuckDB, single writer (the service process). Docker access through the Docker SDK (`docker` package)
  via a socket proxy. Never mount the raw socket into anything except the proxies.
- **Deployment files** live in `deploy/`, add-on definitions in `addons/`, docs in `docs/` (zensical: config `docs/zensical.toml`, pages in `docs/content/`).
- **License:** AGPL-3.0.

## Environment facts
- **Production:** single server, Ubuntu 24.04, mostly NVIDIA GPUs (some AMD), 2–3 users, LAN only.
- **Test server:** Ubuntu 26.04, RTX 5080 (NVIDIA open kernel module), Docker 29 with nvidia-container-toolkit.
  Disk space is limited, so remove images you don't need.
- **Users:** Windows research students, not comfortable with a terminal. Keep the UI and help text plain and task-focused.
