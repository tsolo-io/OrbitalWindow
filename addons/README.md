<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# Add-ons

An add-on is an optional software bundle that users switch on in the **Software** card of their
dashboard. Each add-on is a folder `addons/<key>/` with an `addon.yaml` and any files it needs.
OrbitalWindow loads and validates every folder at startup and when an admin presses
**Reload add-ons** (`/admin/addons`). An invalid add-on is shown there with its error and skipped.

```yaml
key: matlab                 # must equal the folder name: lowercase letters, digits, '-'
label: MATLAB               # shown to users
description: MATLAB R2026b (shared install). Activate your own licence on first start.
restricted: true            # an admin must grant it to a user first (default: false)
apt: [libxt6t64, libnss3]   # Ubuntu packages installed into the user's image
script: install.sh          # optional; run as root during the image build, from this folder
launchers: [ow-matlab.desktop]   # files in this folder, installed to /usr/share/applications
mounts:                     # added when the desktop starts (no rebuild needed)
  - {source: shared/matlab, target: /opt/matlab, read_only: true}   # source relative to OW_ROOT
env: {}                     # extra environment for the desktop container
```

## Rules

- `apt` entries must be plain package names. They are installed with `--no-install-recommends`.
- `script` is a file name in the folder. It runs with `sh` as root, with network access, in the
  folder (copied to `/opt/ow/addons/<key>/`). It may add apt repositories; apt lists are cleaned
  afterwards. Make it idempotent and fail on errors (`set -eu`).
- `launchers` must be named `ow-<name>.desktop`. Every `ow-*.desktop` in
  `/usr/share/applications` is put on the user's Desktop at start, and icons of add-ons that were
  removed disappear.
- `mounts` sources must be inside `shared/` under `$OW_ROOT`. Targets must be absolute and may not
  be inside system folders or `/config`, `/scratch`, `/shared` or `/groups`. If a source folder
  doesn't exist, the desktop refuses to start and tells the user why.
- `env` may not change variables OrbitalWindow sets (`PUID`, `PASSWORD`, `SUBFOLDER`, …).
- Electron apps (VS Code, RStudio, …) must start with `--no-sandbox`: Chromium's sandbox can't
  create its namespaces in a desktop container (ADR 0022).

## When changes take effect

- `apt`, `script` and `launchers` are part of the user's own image `ow-user-<name>`. Changing the
  selection, or any file in the add-on folder, rebuilds the image (users press **Apply**; after
  **Reload add-ons**, affected images are rebuilt automatically). Users restart their desktop to
  get the new image.
- `mounts` and `env` apply at the next start.
- Revoking a grant switches the add-on off for that user. If their running desktop uses one of the
  add-on's mounts, it is stopped.

## Shipped add-ons

| Key | What | Needs on the server |
|---|---|---|
| `matlab` (restricted) | MATLAB runtime libraries, launcher, `/opt/matlab` | `scripts/install-matlab.sh` |
| `python-science` | conda in every shell, JupyterLab and Spyder launchers, `/opt/conda` | `scripts/install-conda.sh` |
| `vscode` | Visual Studio Code from Microsoft's repository | – |
| `devtools` | build-essential, cmake, gfortran, pkg-config, git-gui, gitk, git-lfs | – |
| `rstudio` | R from Ubuntu, RStudio Desktop from Posit | – |
