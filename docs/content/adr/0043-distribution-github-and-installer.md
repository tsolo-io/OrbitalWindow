<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0043 Distribution: public images on GitHub, and an install script

- Status: accepted
- Date: 2026-10-29

## Context
Servers are installed by admins with little Linux experience, often students. Until now a server
needed a copy of `deploy/` from a clone of the office Gitea, and pulled the images from the office
registry (`gitea.tu.tsolo.net`), which is not reachable from outside the office. Each install
meant editing about 20 settings in `.env`, and an update needed a manual copy of new deploy files
next to `make update`; that step was easy to forget. OrbitalWindow is Apache-2.0, so it can be
public.

## Decision
- **GitHub mirror.** Gitea stays the main repository and pushes to
  github.com/tsolo-io/OrbitalWindow. Nothing is changed on GitHub itself.
- **Public images on GHCR.** `.github/workflows/publish.yaml` (run on GitHub only; Gitea reads only
  `.gitea/workflows/`) tests, then builds `ghcr.io/tsolo-io/orbitalwindow` and `…-samba`: `:main`
  on every push to `main`, `:<tag>` and `:latest` on a `v*` tag. `deploy/compose.yaml` uses these
  by default (this replaces the registry in ADR 0021; `OW_IMAGE` and `OW_SAMBA_IMAGE` still
  override it).
- **Releases.** On a tag, the workflow creates a GitHub release with the deploy folder
  (`orbitalwindow-deploy-<tag>.tar.gz`, as foreseen in ADR 0023) and `install.sh`.
- **Install script, `deploy/install.sh`.** Downloaded and run with `sudo`. It does not install
  Docker, Compose or GPU drivers; it checks them and says what is missing:
    - No Docker, or Compose v2, or a stopped daemon: it stops with the command to fix it.
    - GPU: found from PCI display controllers in sysfs, with the bound kernel driver (NVIDIA also
      `nvidia-smi` and the Container Toolkit). A GPU without a working driver is a warning, and the
      server is installed with `OW_GPU_VENDOR=none`, because a missing toolkit would stop the
      whole stack (`compose.nvidia.yaml` reserves the GPU). A discrete GPU wins over Intel graphics.
    - It asks only the host name, the data folder and the time zone, all with defaults, and
      writes `.env` from them: the GPU vendor, the matching `COMPOSE_FILE`, and `OW_AI_ENGINE`
      (`vllm` on NVIDIA, `llamacpp` otherwise).
    - It takes the deploy files from GitHub's archive of the version, so it works for `main` as
      well as for a tag. Default version: the latest release, or `main` while there is none.
- **Updating is running the script again.** With `.env` present it keeps `.env`, `acme.env` and
  the data, replaces the deploy files with the new version's, sets `OW_VERSION` and runs
  `make update`. The deploy files and the images can no longer get out of step.
- **Docs** are published to GitHub Pages by `.github/workflows/docs.yaml`.

## Consequences
- An install is: prepare Docker and the GPU (the vendor pages), download `install.sh`, run it.
- The images must stay public on GHCR (package settings), or servers can't pull them.
- Changing the GPU after an install is a manual edit of `.env` plus **Settings → GPU**, because the
  setting is stored in the database on first start.
- `make init` and copying `deploy/` by hand still work (the guide's *Installing by hand*).
