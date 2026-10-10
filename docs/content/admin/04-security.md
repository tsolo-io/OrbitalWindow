<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# Security

OrbitalWindow is built for a small group of users on a local network. This page reviews the
design's security notes against what is implemented (October 2026), and lists the risks that
remain.

## Model

- **Users are not trusted towards each other.** A user has `sudo` inside their own desktop container
  and may try to reach other users' desktops, files or sessions, or overload the server.
- **Admins are trusted.** They control the server (Docker access is root-equivalent). The UI never
  shows them a user's screen or files, but that is policy, not a technical barrier: an admin can
  reset a user's password and log in as them (audited).
- **LAN only.** Nothing is designed to face the internet.

## Design security notes, reviewed

| Note in the design | Status |
|---|---|
| argon2 password hashes | Done (`argon2-cffi` defaults, rehash on login when parameters change). |
| Login rate limit | Done: 5 failures per 5 minutes per address and per user name; at most 4 password checks run at once (argon2 uses 64 MB each). |
| First admin from `CONTROL_ADMIN_PASSWORD` | Done; used only while there are no users. |
| Audit log | Done: every account, software, group, desktop and settings action, logins and failed logins. Kept when users are deleted. |
| No Docker features beyond what is needed | Done: two socket proxies (read-only for Traefik, sshpiper, Dozzle; read-write for OrbitalWindow only), nothing else mounts the Docker socket; cAdvisor reads cgroups only. |
| Session secret in `state/secret.key`, mode 600 | Done (`state/` itself is mode 700, owned by the service user). |
| LAN only | Unchanged. TLS with a real certificate is ready to switch on (DNS-01). |

## What protects what

- **Sessions:** signed `ow_session` cookie (HttpOnly, Secure, SameSite=Lax, 12 h) checked against the
  database on every use. Changing a password, disabling a user and **logging out** end every session
  of that user, including other open tabs and devices. Open admin pages re-check admin rights on
  every action and refresh.
- **Forms:** login, logout and password change are plain POSTs that must come from the same origin.
  Post-login redirects only go to local paths.
- **Desktops:** only the owner passes forwardAuth for `/desktop/<user>/` (admins too are refused).
  Each desktop is on its own network; nginx inside keeps basic auth with a random secret per start.
  `NET_RAW` is dropped and the number of processes is limited (4096).
- **File manager:** owner-only forwardAuth; runs as the user without capabilities, read-only, on a
  network only Traefik joins (it trusts the user-name header). Inline views of uploaded files are
  sent with `Content-Security-Policy: script-src 'none'` and downloads as attachments, so a file
  another user drops in a shared folder does not run as you (tested with HTML and SVG).
- **Windows sharing:** SMB2+, mandatory signing, no guest, per-user share lists; passwords are never
  stored by OrbitalWindow.
- **SSH:** key-only through the gateway, which blocks an address for 60 minutes after 5 failures;
  the desktop's sshd accepts only a per-user key that lives in the service's state.
- **Admin tools** (cAdvisor, Dozzle): admin-only forwardAuth, on a network desktops never join.

## Remaining risks

| Risk | Why it remains / mitigation |
|---|---|
| The service and the read-write proxy are root-equivalent | Inherent to managing containers. Keep the host patched, the service unreachable except through Traefik, and the images pinned and updated. |
| Shared folders are not private | In scratch and group folders, `sudo` in a desktop bypasses the sticky bit and file modes. The user guide says so; private work belongs in the home folder. |
| Desktop root can use more of the server than its limits suggest | CPU/RAM limits apply only if set; GPU memory is shared first-come, first-served. |
| A compromised sshpiper, Traefik or Dozzle could read container labels and environment | These hold the per-start desktop secrets and the SSH upstream keys. Keep them pinned and updated; they are not exposed beyond the LAN. |
| Same origin for the UI, desktops and file manager | Mitigated by owner-only routing and FileBrowser's `script-src 'none'` on inline views. A separate host name for files would be stronger. |
| Self-signed certificate until DNS-01 is configured | Users click through a browser warning once; switch to a real certificate when the DNS provider is known. |
| Anyone who knows a user name can lock it out for 5 minutes | Trade-off of the per-name rate limit. |
| sshpiper does not verify the desktop's host key | The hop is on the user's private network, reachable only by sshpiper and Traefik. |

## Keeping it secure

- Update OrbitalWindow (new `OW_VERSION`, then `make update`) and **rebuild the desktops** regularly (security
  updates in the webtop base images).
- Review the audit log (System page) now and then, and remove users who have left.
- Keep `.env`, `acme.env` (in the deploy folder) and the backups readable by admins only.
