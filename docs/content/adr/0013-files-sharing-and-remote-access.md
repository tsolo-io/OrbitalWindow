<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0013 Files, sharing and remote access services

- Status: accepted
- Date: 2026-10-09

## Context
Besides Samba, users need to upload and download in the browser, share files with colleagues, and
sometimes use SSH/SFTP (WinSCP, VS Code Remote).

## Decision
(Agreed in the design; implemented in phases 4–5.)
- **Web file manager:** FileBrowser Quantum (`gtstef/filebrowser`, pinned version), one container per
  user running as the user's UID, behind the same forwardAuth with proxy-header auth. The original
  File Browser was archived on 2026-09-01.
- **Sharing:** admin-managed group folders (`setgid`, default ACL) and a shared scratch folder
  (sticky, auto-cleaned after N days). There are no share links.
- **SSH/SFTP:** an opt-in `ssh` add-on (sshd in the desktop, key-only, user `abc`) behind one
  `sshpiper` gateway on port 2222 that routes by container labels. Exec mode is not used, because it
  would give root shells.
- **Disk:** usage measured hourly, soft quotas (warning only).
- **TLS:** a domain name and Let's Encrypt via Traefik DNS-01 once the DNS provider is known.
  Until then, Traefik's self-signed certificate.

## Consequences
- Everything goes through the same login. No service has its own password.
- FileBrowser Quantum v2 is in beta. If it proves unsuitable, the fallback is a minimal
  upload/download page in OrbitalWindow.