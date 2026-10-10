<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0002 Plain Docker Engine via the Docker SDK; DuckDB is the source of truth

- Status: accepted
- Date: 2026-10-09

## Context
The POC managed desktops with shell scripts and per-user compose files. Docker Swarm was tried and
rejected: it can't pass GPUs with `--gpus`/devices in the same way, can't set a fixed MAC per endpoint
(needed for MATLAB licences), and would need a registry for locally built images.

## Decision
- OrbitalWindow manages containers with the Docker SDK for Python (`docker` package) on a plain
  Docker Engine. There are no per-user compose files.
- DuckDB holds the desired state (users, chosen desktop, limits). Docker holds only runtime state.
- **Start** always creates a fresh container (`ow-<user>`) from the current spec. **Stop** removes the
  container and the user's network. The home folder on the host is kept.
- More than one server later means OrbitalWindow manages several Docker hosts, not Swarm.

## Consequences
- A spec change (limits, mounts, image) takes effect at the next start, with no migration step.
- Anything a user installs outside their home folder is lost at stop. Software that should persist
  belongs in the per-user image (see [0012](0012-software-addons-and-per-user-images.md)).
- The container spec is built by one pure function (`desktops.build_spec`) and covered by a
  snapshot test.