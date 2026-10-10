<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0010 Samba in a container, kept in sync by exec helpers

- Status: accepted
- Date: 2026-10-09

## Context
Windows users reach their files as `\\server\<user>`. Each Samba user must be a Unix user whose UID
equals the desktop's PUID, so files have the right owner. Samba keeps its own password hashes.

## Decision
- An Alpine + Samba image (`deploy/samba/`) publishes port 445. `/var/lib/samba` (passdb) is a
  named volume, so passwords survive restarts.
- OrbitalWindow writes `state/samba/users.json` (names and UIDs only), mounted read-only into Samba.
  - `ow-sync` (via Docker exec) creates or removes Unix users and per-user shares, then reloads.
    It also runs at container start, because the container's `/etc/passwd` is not persistent.
  - `ow-passwd <user>` reads the password from **stdin** and runs `smbpasswd`.
- `smb.conf` uses the settings verified in the POC: SMB2+ only, mandatory signing, no guest,
  per-user share enumeration, and dot files hidden.

## Consequences
- No plaintext password is stored anywhere. The password is passed to Samba only when it is set.
- If Samba is down when a password changes, the Samba password is not updated. The UI says so and
  the admin must reset the password again. The other steps are not rolled back.