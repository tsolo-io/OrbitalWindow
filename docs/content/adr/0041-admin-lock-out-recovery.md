<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0041 Lock-out recovery: a one-off container while the service is stopped

- Status: accepted
- Date: 2026-10-19

## Context
When the only administrator forgets the password, or is disabled or deleted, nobody can log in.
`CONTROL_ADMIN_PASSWORD` is used only while there are no users. The only way back was to edit DuckDB
by hand, which skips the audit log and Samba (#42). A customer's system administrator may have to
do this, so it must be one simple step.

## Decision
- `make reset-admin-password [NAME=admin]` in the `deploy/` folder. It stops the `orbitalwindow`
  container (DuckDB has one writer), runs `orbitalwindow-reset-admin <name>` in a one-off
  container of the same compose service (same image, user, `OW_ROOT`, environment and socket
  proxy; Traefik routing off), and always starts the service again.
- `Service.recover_admin`: a new generated password (printed once), the account enabled and made
  an administrator (`NAME`, because the shell sets `USER`), other sessions signed out, an audit
  entry (`system`, `user.recover`). A deleted user is created again as an administrator.
- The Samba password is set in the same run. Any Samba failure is a warning only: the reset
  stands, and the password can be set again on the Users page.

## Consequences
- One command gets an administrator back in; the in-memory login blocks are cleared by the
  restart.
- The service is down for a few seconds; desktops and the AI session keep running.
- Anyone who can run `make` in the deploy folder (Docker access) can take over an account; that
  access already means full control of the server.
