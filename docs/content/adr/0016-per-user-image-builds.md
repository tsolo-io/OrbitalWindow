<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0016 How per-user images are built and when they are stale

- Status: accepted
- Date: 2026-10-09

## Context
[0012](0012-software-addons-and-per-user-images.md) decided to build a per-user image
`ow-user-<name>` from the chosen desktop, the user's add-ons and their own apt packages. This record
covers how it is built and when it counts as out of date.

## Decision
- **Hash:** a per-user image is described by a SHA-256 over the base image's Docker ID, each active
  image-affecting add-on (key + hash of all files in its folder) and the sorted package list. It is
  stored with the last good build (`user_image.pkg_hash`). A different hash means "changes not
  installed". No add-ons/packages that affect the image → no per-user image; the desktop image is used.
- **Build context:** a temporary folder with a generated Dockerfile and copies of the selected add-on
  folders. Add-ons come first in key order, one layer each, so users with the same add-ons share
  Docker's layer cache. The user's packages come last. Apt lists are removed in every layer.
- **Failures:** Docker tags only on success, so a failed build leaves the last good image in place,
  and its log is kept in `builds`. Starting uses the last good image built on the user's current
  desktop; if there is none, the start is refused with a reason.
- **Package index:** the base image ships without apt lists, so after each base build a throwaway
  container runs `apt-get update && apt-cache pkgnames` and fills `pkg_index`. Packages are
  validated (and autocompleted) against it. Switching desktop drops packages the new desktop lacks.
- **Automatic rebuilds:** after a base image is rebuilt, per-user images built on it are queued.
  After **Reload add-ons** or disabling an add-on, images whose hash changed are queued.
- Superseded (dangling) images we built are pruned after each successful build.
- Phase 2 also ships the admin **Desktops** and **Add-ons** pages and the per-user **Software
  access** grants, because building and granting can't be tested without them. Phase 3 adds the rest
  of the admin pages.

## Consequences
- A user's image only changes when they press Apply (or an admin action makes it stale), and the new
  software appears at the next desktop start; the dashboard says so.
- The package index reflects the archive at build time; a rebuild refreshes it.
