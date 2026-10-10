<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0014 Admins never see users' desktops or files

- Status: accepted
- Date: 2026-10-09

## Context
Admins manage accounts and containers. Users keep research data and licences in their desktops.

## Decision
- forwardAuth lets only the owner into `/desktop/<user>/` and (later) `/files/<user>/`. Being an
  admin gives no access to them.
- Admins are ordinary users with a flag. They can have and open their **own** desktop.
- Admin pages show container state and metrics, never screen contents or file listings.

## Consequences
- Supporting a user means sitting with them (or a screen share outside OrbitalWindow), not opening
  their desktop.
- Admins still control the host. This is a UI policy, not protection against a malicious admin.