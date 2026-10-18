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
All six design phases are implemented, one stacked PR per phase for ticket #2 (PRs #4–#9). The POC is not
carried over: no migration, no backward compatibility, no redirects. Decisions and their reasons are recorded as
ADRs in `docs/content/adr/`; add one for every new architectural decision. Admin guide: `docs/content/admin/`;
user help pages (also served at `/help`): `docs/content/user/`.
Later tickets, all merged by 2026-10-11:
- **AI:** #15–#18 (model library, fit check, vLLM session, Open WebUI; ADRs 0024–0028) and #21 (coding
  benchmarks, ADR 0029). Fixes: vLLM tool calling (#30) and hybrid or thinking models such as Qwen3.5 (#31).
- **Deploy and CI:** #3 (CI) and #14 (deploy Makefile, ADR 0023).
- **Server status:** #22 (node-exporter, ADR 0025) and temperatures (#33).
- **Local time:** #32. Times are stored in UTC and shown in `TZ` through `ui.common.local_time`. A test
  stops `ui/` pages from formatting times themselves.

Open: TLS (built, but no domain yet), remote access (not decided; see design.md *TLS with a domain*),
and MATLAB in a desktop (needs a licence). Martin's testers will log new tickets.

## Project conventions
- **Build:** hatch / hatchling, src layout (`src/orbitalwindow/`), Python 3.14 (service image `python:3.14-slim`).
- **Libraries:** pydantic / pydantic-settings for config and validation; `httpx2` (not `httpx`) for HTTP clients.
- **Hatch environments** (wrapped by the `Makefile`; `make help` lists targets):
  - `make test`: pytest (built-in `hatch-test` env).
  - `make format` / `make lint` (or `hatch run lint:fmt` / `hatch run lint:check`): ruff.
  - `make start` / `update` / `stop` / `logs` / `status`: the compose stack, run through `deploy/Makefile` (needs `deploy/.env`).
  - `hatch run docs:build` / `hatch run docs:serve`: zensical, run as `zensical build -f docs/zensical.toml`.
- **Database:** DuckDB, single writer (the service process). Docker access through the Docker SDK (`docker` package)
  via a socket proxy. Never mount the raw socket into anything except the proxies.
- **Images:** the service image is built from the root `Dockerfile` (desktop image context, add-ons and help
  pages baked in); Samba from `deploy/samba/`. CI (`.gitea/workflows/build.yaml`) publishes both to
  `gitea.tu.tsolo.net/kuat/orbitalwindow{,-samba}` (`:nightly` on every push, `:main`, `:<git tag>`).
- **Compose:** `deploy/compose.yaml` is the production stack (published images, `OW_VERSION`);
  `deploy/compose.dev.yaml` overrides it to build from the checkout. `make start` = dev, `make start PROD=1` = prod.
- **Servers** need only a copy of `deploy/`: `deploy/Makefile` (`make init`, `start`, `update`, `install-conda`,
  `install-matlab`, `backup`, `export-cert`) picks the compose files from `.env`; scripts are in `deploy/scripts/` (ADR 0023).
- **Deployment files** live in `deploy/`, add-on definitions in `addons/`, docs in `docs/` (zensical: config `docs/zensical.toml`, pages in `docs/content/`).
- **License:** Apache-2.0.

### Code style
- **Variable names:** no single-letter names in a `for` loop whose body spans lines or in a comprehension that
  wraps over several lines; a one-line comprehension may keep short names. Exceptions are caught `as exc`.
- **Docstrings:** document every function and method (private helpers and nested UI handlers too): a one-line
  summary, a long description only if needed (after a blank line), then Google style `Args:` (no types; they
  are in the signature), `Returns:` and other sections such as `Raises:` when relevant. Test functions only
  need a one-line docstring.

## Environment facts
- **Production:** single server, Ubuntu 24.04, mostly NVIDIA GPUs (some AMD or Intel), 2–3 users, LAN only.
- **Test server:** Ubuntu 26.04, RTX 5080 (NVIDIA open kernel module), Docker 29 with nvidia-container-toolkit.
  Disk space is limited, so remove images you don't need.
- **Wired:** Ubuntu 24.04, Intel Arc A770 (`i915`), `OW_GPU_VENDOR=intel`. Mes Antilles runs here.
- **Users:** Windows research students, not comfortable with a terminal. Keep the UI and help text plain and task-focused.
