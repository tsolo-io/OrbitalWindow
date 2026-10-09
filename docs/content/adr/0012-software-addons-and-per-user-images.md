<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0012 Optional software as add-on folders, installed by per-user image builds

- Status: accepted
- Date: 2026-10-09

## Context
Users need different software (MATLAB, conda, VS Code, R, …). An `apt install` inside a container is
lost when it is recreated, and installing at every start is slow.

## Decision
(Agreed in the design; implemented in phase 2.)
- Admins add a desktop by entering a LinuxServer webtop tag. The service builds `ow-desktop:<tag>` from
  `deploy/image/` (a slim base), then records the image's apt package names for validation.
- Optional software is described by **add-ons**: folders `addons/<key>/` with an `addon.yaml`
  (apt packages, install script, launchers, mounts, env), versioned in git and validated at startup.
  Restricted add-ons (such as MATLAB) need an admin grant.
- When a user's desktop choice, add-ons or package list changes, a per-user image
  `ow-user-<name>:latest` is built in a one-at-a-time background queue. A failed build keeps the last
  good image.

## Consequences
- Starting stays fast. Build logs are shown in the UI.
- Disk use grows with each user's image layers. Unused images are removed.