<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0034 Other distributions: ow-pkg over apt, apk, dnf and pacman

- Status: accepted
- Date: 2026-10-16

## Context
Ticket #40: terminal-only containers use LinuxServer base images, "Ubuntu and other distro's"
(Martin). LinuxServer publishes base images for Ubuntu, Debian, Alpine, Fedora and Arch, and webtop
tags for the same distributions. Until now, every image step assumed apt:

- the desktop Dockerfile;
- the package index (`apt-cache pkgnames`);
- users' packages and add-on `apt:` lists;
- package names checked as Debian names (lower case).

## Decision
- **`ow-pkg`** (`deploy/image/root/usr/local/bin/ow-pkg`) is one POSIX shell interface to
  the package manager:
  - `manager` prints apt, apk, dnf or pacman;
  - `install` installs packages; apt uses no recommends, dnf no weak dependencies, and pacman
    `-Syu --needed`, because Arch only supports installing on an up-to-date system;
  - `install-any` installs what exists and reports the rest;
  - `index` prints all package names;
  - `clean` removes caches and lists.
  It is in every base image. Per-user builds also copy it in, so images built before it existed
  still work.
- **Base tools:** `deploy/image/base-packages.sh desktop|terminal` installs the same tools on every
  system, with each distribution's names.
  - Required (the build fails without them): bash, tmux, sudo, git, curl, nano, less, htop, rsync,
    unzip, Python, procps and libcap. Alpine also gets shadow (`usermod` and `groupadd` for the init
    hook) and coreutils (`install`).
  - Extras (skipped where missing): nvtop, plus Mesa, Vulkan tools and fonts for desktops.
  - Terminal-only images also set up `abc` (bash, unlocked for SSH keys, sudo), as ADR 0033 did
    for Ubuntu.
  Both Dockerfiles use the script, so webtop tags of other distributions work too.
- **`images.pkg_manager`** is recorded when the package index is built: the first line of
  `ow-pkg manager && ow-pkg index`.
- **Add-ons:**
  - new `apk`, `dnf` and `pacman` lists beside `apt`, and an optional `managers` list;
  - an add-on works where `managers` says. Without it, it works where it lists packages; with
    only a script, on apt (scripts so far are written for Ubuntu); with neither, everywhere;
  - elsewhere it is greyed out as *Not available for Fedora* (or Alpine, Arch) and left out of
    the image plan;
  - scripts get `OW_PKG_MANAGER`.
  - Shipped: `ssh` and `devtools` list packages for all four. `python-science` works on apt, dnf
    and pacman (conda needs glibc; its hook finds Fedora's `/etc/bashrc`). VS Code, RStudio and
    MATLAB stay Ubuntu/Debian only, and they need a desktop anyway.
- **Users' packages:** names may use upper case, `_` and `@` (Fedora's `ImageMagick`). Search and
  matching ignore case, and the name is stored as the system spells it.

## Consequences
- Tested by building the terminal-only image on LinuxServer's Alpine 3.22, Fedora 42, Arch, Debian
  bookworm and Ubuntu noble base images. On each, a per-user image with `ssh` and `devtools` was
  built and run. All got healthy, mapped `abc` to `PUID`, created group folders' groups, set the
  user's variables, and accepted SSH and SFTP key logins. gcc, gfortran and cmake were installed.
- Base images need a tag: LinuxServer base images don't all publish `latest`.
- Add-on authors who want other systems add package lists (and make scripts branch on
  `OW_PKG_MANAGER`).
