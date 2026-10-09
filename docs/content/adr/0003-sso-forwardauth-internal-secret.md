<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0003 Single sign-on with Traefik forwardAuth and a per-start internal secret

- Status: accepted
- Date: 2026-10-09

## Context
In the POC each desktop had its own basic-auth password (the user's). Users had to log in twice, and
the password lived in `.env` files. The webtop image only offers nginx basic auth (`CUSTOM_USER` +
`PASSWORD`) on port 3000.

## Decision
- At login, OrbitalWindow sets `ow_session`: an itsdangerous-signed token with the user id and a
  session version. Cookie flags: HttpOnly, Secure, SameSite=Lax, Path=/, 12-hour lifetime.
- Each desktop router uses the forwardAuth middleware `ow-auth-desktop` →
  `http://orbitalwindow:8000/auth/desktop`. It allows the request only when the session user is the
  owner named in `/desktop/<owner>/…`. No cookie → 302 to `/login`; anyone else → 403. Admins are
  not let into other users' desktops ([0014](0014-admins-never-see-user-desktops.md)).
- The desktop's `PASSWORD` is a random secret generated at every start. A per-router Traefik
  `headers` middleware injects the matching `Authorization: Basic …`, so basic auth stays on inside
  the container and is invisible to the user.
- Every session lookup checks `users.session_ver` and `disabled`. Changing a password or disabling a
  user bumps the version, which signs out existing cookies at once.

## Consequences
- Bypassing Traefik needs the internal secret. Only processes with Docker API access can read it
  (it is in the container's labels and environment).
- The desktop is served from the same origin as OrbitalWindow. Content in a desktop runs with the
  owner's cookie, but only the owner can reach that desktop, so this gives a user no extra access.
- The path match is exact (`/desktop/user1` never matches `/desktop/user10`). The Traefik rule uses
  `Path(/desktop/u) || PathPrefix(/desktop/u/)`, plus a redirect that adds the trailing slash.