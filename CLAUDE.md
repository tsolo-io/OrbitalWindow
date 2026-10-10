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
Built in phases (see `docs/content/design.md`), one stacked PR per phase for ticket #2. The POC is not carried
over: no migration, no backward compatibility, no redirects. Decisions and their reasons are recorded as ADRs in
`docs/content/adr/`; add one for every new architectural decision.

## Project conventions
- **Build:** hatch / hatchling, src layout (`src/orbitalwindow/`), Python 3.14 (service image `python:3.14-slim`).
- **Libraries:** pydantic / pydantic-settings for config and validation; `httpx2` (not `httpx`) for HTTP clients.
- **Hatch environments** (wrapped by the `Makefile`; `make help` lists targets):
  - `make test`: pytest (built-in `hatch-test` env).
  - `make format` / `make lint` (or `hatch run lint:fmt` / `hatch run lint:check`): ruff.
  - `make start` / `stop` / `logs` / `status`: the compose stack in `deploy/` (needs `deploy/.env`).
  - `hatch run docs:build` / `hatch run docs:serve`: zensical, run as `zensical build -f docs/zensical.toml`.
- **Database:** DuckDB, single writer (the service process). Docker access through the Docker SDK (`docker` package)
  via a socket proxy. Never mount the raw socket into anything except the proxies.
- **Deployment files** live in `deploy/`, add-on definitions in `addons/`, docs in `docs/` (zensical: config `docs/zensical.toml`, pages in `docs/content/`).
- **License:** Apache-2.0.

## Environment facts
- **Production:** single server, Ubuntu 24.04, mostly NVIDIA GPUs (some AMD), 2–3 users, LAN only.
- **Test server:** Ubuntu 26.04, RTX 5080 (NVIDIA open kernel module), Docker 29 with nvidia-container-toolkit.
  Disk space is limited, so remove images you don't need.
- **Users:** Windows research students, not comfortable with a terminal. Keep the UI and help text plain and task-focused.
