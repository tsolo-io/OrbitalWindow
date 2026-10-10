<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0021 Published images and the production / development compose files

- Status: accepted
- Date: 2026-10-10

## Context
Ticket #3: build the image on Gitea with `.gitea/workflows/build.yaml` (which uses
`Tsolo/container-build-publish-action`), and provide a development compose file and an example
production compose file. The action builds the `Dockerfile` in the repository root with BuildKit and
tags `gitea.tu.tsolo.net/<owner>/<repo>:nightly` on every run, plus the branch (`main`) or git tag.

## Decision
- **Root `Dockerfile`** for the service (moved from `deploy/Dockerfile`). It bakes in everything the
  service reads at runtime: the desktop image build context (`deploy/image`), the add-ons and the help
  pages. A server therefore needs no source checkout. An allow-list `.dockerignore` keeps the build
  context to what the Dockerfile copies.
- **Samba image** published too, by a second step of the same action (`context: deploy/samba`,
  `name: orbitalwindow-samba`).
- **`deploy/compose.yaml` is the production stack:** published images at `${OW_VERSION:-main}`, no
  source mounts. **`deploy/compose.dev.yaml`** overrides it to build both images from the checkout
  (`orbitalwindow:dev`, `orbitalwindow-samba:dev`) and mounts `deploy/image` and `addons/` over the
  baked-in copies for live editing. `make start` uses both; `make start PROD=1` only the production file.
  The optional `compose.nvidia.yaml` and `compose.tls.yaml` work with either.
- Custom add-ons in production: mount a folder over `/opt/orbitalwindow/addons` with a compose
  override.

## Consequences
- One stack definition; development differs only where it must (build, live mounts), so the two
  cannot drift apart.
- Upgrading a server is `OW_VERSION=<tag>` plus `make start PROD=1` (pulls and recreates what changed).
- Changing add-ons on a production server means a new image (or an override mount), not a git pull.
