<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0020 Desktop readiness, the desktop tab, and recovering from DuckDB fatal errors

- Status: accepted
- Date: 2026-10-10

## Context
Testing on the VM showed three problems:

1. A desktop was reported *Running* (and routed by Traefik) as soon as nginx in the container
   answered. Selkies, the stream server behind it, starts up to a minute later. A browser that
   connected in that window ended up with a stream page that kept failing ("websocket
   disconnected", 400 responses) until reloaded. The same happened to a desktop tab left open while
   the desktop was stopped and started, e.g. when switching from MATE to XFCE.
2. OrbitalWindow pages sent `Cross-Origin-Opener-Policy: same-origin`, which makes the browser drop
   the name of a tab opened from them, so "Open desktop" could not reuse its tab.
3. A rebuild's `DELETE FROM pkg_index …` failed with "Failed to delete all rows from index" (the
   table's secondary index had gone out of step). DuckDB then invalidates the database for the rest
   of the process, so every page failed until the service was restarted by hand.

## Decision
- The desktop image's healthcheck calls Selkies itself (`127.0.0.1:8082${SUBFOLDER}api/status`), so
  *Running*, **Open desktop** and Traefik's route all wait for the stream server.
- **Open desktop** opens a named tab (`ow-desktop-<user>`), so pressing it again reloads that tab.
  OrbitalWindow sends `Cross-Origin-Opener-Policy: same-origin-allow-popups`, and none under
  `/desktop/`, so the name survives.
- The "Your desktop is not running" page (where a stream page lands while the desktop is stopped)
  checks every 3 s and opens the desktop by itself once it runs again.
- **Each desktop start gets its own URL path**, `/desktop/<user>/<start id>/` (8 random hex
  characters, label `ow.session`, `SUBFOLDER` of that container). The stream client keeps its settings
  in `localStorage` under a key made from the page URL, and the browser caches per path. With a
  fixed `/desktop/<user>/`, a new container (e.g. after switching MATE → XFCE) inherited state from
  the previous one and refused the stream connection with 400 until the user cleared the browser
  (an incognito window worked). Traefik routes only the running container's path;
  `/desktop/<user>/` and old paths are handled by OrbitalWindow, which redirects to the current
  path, or waits and then goes there. forwardAuth is unchanged (it checks the `<user>` segment).
- Migration 5 drops the secondary index on `pkg_index` (a scan of ~80k rows per desktop is fast).
- On any `duckdb.FatalException`, the service logs it and exits; Docker's restart policy starts it
  again and DuckDB recovers from its WAL, instead of every request failing.
- The Traefik access log is an option (`OW_TRAEFIK_ACCESS_LOG` in `deploy/.env`), documented in the
  admin guide for connection problems.

## Consequences
- After a start, the dashboard shows *Starting…* about a minute longer than before, but the stream
  works the first time.
- Stream settings a user changes in the Selkies sidebar apply to the current start only.
- A DuckDB fatal error costs a restart of a few seconds instead of an outage until someone notices.
