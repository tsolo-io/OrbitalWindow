<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0008 Login and password forms are plain HTTP POSTs, not NiceGUI events

- Status: accepted
- Date: 2026-10-09

## Context
NiceGUI handles UI events over a WebSocket, so an event handler cannot set an HttpOnly cookie.
NiceGUI inputs also send every keystroke to the server.

## Decision
- `/login` and **Change my password** render ordinary HTML `<form method=post>` elements with plain
  inputs. They post to FastAPI routes (`/auth/login`, `/account/password`), which set or re-issue the
  cookie and redirect.
- These routes refuse a POST whose `Origin` doesn't match `Host` (CSRF). The cookie is SameSite=Lax as
  a second line of defence.
- Post-login redirect targets are limited to local paths with a safe character set (no open redirect).
- NiceGUI pages check the session when they load. Every event handler checks it again before acting,
  so a password change or a disabled account takes effect without a page reload.

## Consequences
- Passwords travel only in the form submission, never over the UI WebSocket.
- Results are shown after the redirect from a fixed set of messages, so no request text is reflected.