<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0045 An LLM gateway (LiteLLM) in front of the models

- Status: proposed
- Date: 2026-11-04

## Context
Users' programs call the model with an Open WebUI API key (`sk-…`) at `https://<host>:8443/api`
(ADR 0028, #61). Open WebUI checks the key and sends the request on to the AI session. This was the
easy way to get a personal key with the chat, but it has limits:

- **Open WebUI decides which endpoints exist.** It has only `/api/chat/completions` and
  `/api/models`. Code completion in editors (*fill-in-the-middle*, FIM) uses the legacy
  `/v1/completions` endpoint (Zed, Continue in VS Code) or llama.cpp's `/infill` (llama.vscode).
  Users can not get these (#79). Today FIM works only with the admin `ow-` key on a public session
  (`https://<host>/v1`), and users must not get that key.
- **The keys are in a second user database.** OrbitalWindow does not own them, and they work only
  while `ow-openwebui` runs.
- **There is no per-user limit.** FIM sends many short requests while a user types. One user can
  fill the GPU, and the chat gets slow for everyone.
- **There is one model and one URL.** A second model (for example a small coder model for FIM next
  to the chat model, or a second GPU node) needs a new URL and new client settings.

## Decision
- **A gateway, `ow-llm-gateway`, is the only API for programs.** It runs LiteLLM (proxy) on the
  `ow-ai` network (no internet). Traefik serves it at `https://<host>:8443/v1` (same entrypoint
  and certificate as the chat). It sends each request to the model by the model name:
  `/v1/chat/completions`, `/v1/completions` (FIM), `/v1/embeddings` and `/v1/models`.
- **OrbitalWindow owns the keys.** Each user gets a LiteLLM *virtual key*. OrbitalWindow makes,
  blocks and deletes it with the gateway's admin API and its master key (in `state/`, like the
  other secrets). It blocks the key when the user is disabled and deletes it when the user is
  deleted, and it writes these changes to the audit log. The dashboard shows the key as it shows
  the `sk-` key now. Only the gateway and OrbitalWindow know the models' own keys.
- **Limits per key.** Each key has a request and token rate limit, set by the admins on the AI page.
  This keeps FIM from one user from slowing the chat for the others.
- **Routing.** The gateway's model list comes from the running AI sessions. One session stays the
  normal case (ADR 0027), but a second session (a coder model for FIM, or another node) only adds
  a model to the list. Clients keep the same URL and key.
- **Open WebUI goes direct.** The chat keeps its own connection to the session with the session's
  key, as now. Chat usage is therefore not counted in the gateway. The `sk-` keys stay for one
  release, with a note on the dashboard, and are then turned off (`ENABLE_API_KEYS=false`).
- **Offline and pinned.** The image is pinned by digest. The gateway uses its local model price list
  (`LITELLM_LOCAL_MODEL_COST_MAP=True`) and sends no telemetry.
- **Database.** LiteLLM keeps virtual keys, budgets and usage in PostgreSQL: its own database
  `litellm` and role on the shared server `ow-db` (ADR 0046), in the system backups.

### To check before this ADR is accepted
A short test on the GPU node, with vLLM and with llama.cpp:
1. `/v1/completions` with a FIM prompt goes through unchanged (Zed and Continue).
2. Virtual keys: make, block and delete from OrbitalWindow; a blocked key gets 401.
3. Rate limits per key work, and no request goes to the internet.
4. `/infill` for llama.vscode. A LiteLLM pass-through endpoint can forward it, but pass-through
   with key checks is an Enterprise feature. If it is needed, OrbitalWindow's forwardAuth checks
   the key with the gateway (`/key/info`), and Traefik sends `/v1/infill` to the llama.cpp session.
   Otherwise llama.vscode is not supported, and the help names Continue.
5. Memory and start time of the gateway and the database.

## Alternatives
The search was on 2026-11-04. Most comparisons are written by the vendors, so the details must be
checked again in the test.

- **Own keys in OrbitalWindow** (a table of key hashes, forwardAuth, Traefik to the model): the
  least new code to run, and no new containers. But there is no routing by model name, no limits
  and no usage numbers, and we would write these ourselves later.
- **Bifrost** (Maxim AI, Go, Apache-2.0): virtual keys, budgets and rate limits are in the open
  source version. It can keep its configuration in SQLite, so no database container is needed. It
  is the strongest alternative. Not chosen now: support for `/v1/completions` to a custom
  OpenAI-compatible backend is not clear from its documentation, and it is younger, with a smaller
  user base. If the LiteLLM test fails on the database, memory or `/v1/completions`, test Bifrost
  next.
- **Portkey gateway** (MIT): the gateway is open source, but key management and storage are mostly
  in the paid cloud. Palo Alto Networks bought Portkey in 2026.
- **Kong AI Gateway, Envoy AI Gateway, Apache APISIX**: general API gateways with AI plugins. Kong
  needs the Enterprise edition for most of this, and Envoy AI Gateway is made for Kubernetes. Too
  heavy for one server.
- **TensorZero, GoModel, LLM Gateway (AGPLv3)**: smaller or with a different aim (evaluation and
  optimisation, or a hosted product). Not checked further.

## Consequences
- **One more container** (`ow-llm-gateway`), on the `ow-ai` and `ow-db` networks. It needs
  updates.
- **Supply-chain risk.** On 2026-03-24, two LiteLLM releases on PyPI (1.82.7 and 1.82.8) had a
  credential-stealing payload for about 40 minutes. The image is therefore pinned by digest,
  updated on purpose only, and has no internet access. It knows the models' keys, but no user
  passwords.
- **One API for programs**, with the same URL and key whatever model or engine (vLLM, llama.cpp)
  runs, and room for more models and nodes later.
- **The help and the dashboard change**: one base URL (`https://<host>:8443/v1`) for chat, FIM and
  embeddings, with examples for Zed, Continue, Python and MATLAB.
- **Chat usage is not counted** in the gateway while Open WebUI goes direct. If per-user numbers
  for the chat are needed later, Open WebUI can use the gateway with one service key and send the
  user's name.
