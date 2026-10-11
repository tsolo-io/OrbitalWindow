<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0023 The deploy folder is the install, with its own Makefile

- Status: accepted
- Date: 2026-10-10

## Context
Ticket #14. Servers are installed by copying `deploy/`, but running it needed things from outside
that folder:
- The compose-file logic (`compose.nvidia.yaml` on NVIDIA servers, `compose.tls.yaml` with ACME) lived
  in the repository's root `Makefile`. On the GPU node the stack was started with plain
  `docker compose`, and the admin pages showed no GPU numbers.
- The MATLAB, conda and backup scripts lived in `scripts/`, so they weren't on the server at all.

## Decision
- **`deploy/Makefile`** is what a server runs, from the deploy folder:
  - `init`: creates `.env` from `.env.example` with mode 600 and a generated first admin password.
  - `start`, `update` (pull, then recreate), `stop`, `restart`, `logs`, `status`.
  - `install-conda`, `install-matlab`, `backup`, `export-cert`.
- **Compose files** are chosen from `.env` (`OW_GPU_VENDOR`, `OW_ACME_DNS_PROVIDER`). `DEV=1` adds
  `compose.dev.yaml`.
- **The root `Makefile`** keeps the developer targets. Its stack targets call `deploy/Makefile`
  (with `DEV=1` unless `PROD=1`), so the compose-file logic exists once.
- **Scripts** move to `deploy/scripts/`. The install scripts no longer create the target folder on
  the host (`docker -v` does), so a member of the `docker` group runs them without `sudo`.
- **Bundle:** for now, a plain copy of `deploy/` from a fresh clone. The guide recommends keeping
  it apart from the data folder (`OW_ROOT`), so it can be replaced on an update.
- **`COMPOSE_FILE`** in `.env` stays as the way to use plain `docker compose` in that folder.

## Consequences
- **Prerequisites:** a server needs `make` (`apt install make`), which is already in the
  host-preparation step of the install guide.
- **Updates:** a new release's deploy files are copied over the folder; `.env` and `acme.env` are
  not in the repository, so they survive.
- **Later:** a release tarball of `deploy/` (made in CI) can replace the copy, with no change to the
  folder's layout.
