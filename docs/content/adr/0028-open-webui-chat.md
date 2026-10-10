<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0028 The AI chat: Open WebUI on its own port, signed in through OrbitalWindow

- Status: accepted
- Date: 2026-10-10

## Context
Ticket #18: a chat page for every user in front of the AI session (#17), and a way for code to call
the model with a personal key. There is one login (OrbitalWindow's), and only OrbitalWindow admins
may administer the chat. Martin prefers a path, but a separate port or host is acceptable.

## Decision
- **Open WebUI** (`OW_OPENWEBUI_IMAGE`, default `ghcr.io/open-webui/open-webui:v0.11.4`) runs as
  `ow-openwebui` while the session is starting or running, and is removed when it stops.
  - It sits on the session network `ow-ai` (no internet; `OFFLINE_MODE`).
  - Its data (users, chats, keys) is in `$OW_ROOT/state/openwebui`, mounted at `/data` with
    `DATA_DIR`. The image keeps its embedding and speech models under `/app/backend/data/cache`,
    and a mount there would hide them.
  - Its signing key is kept in `state/openwebui_secret`, so sign-ins survive a recreate.
  - The environment is the whole configuration (`ENABLE_PERSISTENT_CONFIG=false`). A spec hash
    recreates the container when it changes (new API key, image or host name).
- **Own port 8443** (Traefik entrypoint `ai`, same certificate). Open WebUI v0.11 still can't run
  under a path. OrbitalWindow's session cookie is sent there too, because cookies don't depend on the
  port.
- **Sign-in:** forwardAuth `/auth/ai` admits every logged-in user and always returns three headers.
  Open WebUI trusts them (`WEBUI_AUTH_TRUSTED_*_HEADER`):
  - `X-OW-Email: <user>@orbitalwindow.local`;
  - `X-OW-Name` (the display name);
  - `X-OW-Role` (`admin` for OrbitalWindow admins, else `user`). Open WebUI applies it at every
    sign-in, so the first account can't become admin by accident, and demotions apply.

  The login form and sign-up are off. Open WebUI rejects a token that doesn't match the trusted
  e-mail, so switching OrbitalWindow users in one browser switches the chat user.
- **Keys for code:** every user may create an Open WebUI API key (`ENABLE_API_KEYS`,
  `USER_PERMISSIONS_FEATURES_API_KEYS`). Requests with `Authorization: Bearer sk-…` under `/api`
  take a second router without forwardAuth; a headers middleware blanks `X-OW-*`, and Open WebUI
  checks the key itself. The OpenAI-compatible endpoints are `https://<host>:8443/api/chat/completions`
  and `/api/models`, which work from Python (`openai`) and MATLAB (`openAIChat` with `BaseURL`).
- **Connection to vLLM:** `OPENAI_API_BASE_URL=http://ow-ai-model:8000/v1` with the session's API
  key. Ollama is off. `BYPASS_MODEL_ACCESS_CONTROL` lets every user see the one model, and the
  evaluation "arena" model is off.
- **Dashboard:** **Open chat** (a new tab) and a link to the help page, while the model runs.

## Consequences
- **Port 8443** must be open on the host. Browsers ask once to accept the self-signed certificate
  for this address too, unless it is installed on the PCs or a real certificate is used (the TLS
  override covers both entrypoints).
- **Chats stay** in `state/openwebui` (part of the state backup) when the session stops; the chat is
  only unavailable meanwhile.
- **Checked on the GPU node:**
  - no session → login redirect; sign-in as user and as admin with the right roles;
  - a chat answered by the model; switching users in one browser;
  - a key works without a session; a bad key gets 401;
  - forged `X-OW-*` headers don't change the user or role.
