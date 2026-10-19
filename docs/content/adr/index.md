<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# Architecture Decision Records

Each record captures one decision: its context, what we chose and what follows from it. Records are
never rewritten once accepted. A changed decision gets a new record that **supersedes** the old one.

Format: [MADR](https://adr.github.io/madr/) (short form). File name: `NNNN-title-with-dashes.md`.
Status is one of *proposed*, *accepted*, *superseded by NNNN*.

| # | Decision | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | accepted |
| [0002](0002-plain-docker-engine-via-sdk.md) | Plain Docker Engine via the Docker SDK; DuckDB is the source of truth | accepted |
| [0003](0003-sso-forwardauth-internal-secret.md) | Single sign-on with Traefik forwardAuth and a per-start internal secret | accepted |
| [0004](0004-per-user-networks.md) | One Docker network per user | accepted |
| [0005](0005-docker-access-through-socket-proxies.md) | Docker access only through socket proxies; the service is not root | accepted |
| [0006](0006-same-path-data-root.md) | `$OW_ROOT` is mounted at the same path in the service | accepted |
| [0007](0007-configuration.md) | Process config from the environment (pydantic-settings), runtime settings in the DB | accepted |
| [0008](0008-auth-forms-are-plain-posts.md) | Login and password forms are plain HTTP POSTs, not NiceGUI events | accepted |
| [0009](0009-duckdb-single-writer.md) | DuckDB with one writer process and one lock; UTC timestamps | accepted |
| [0010](0010-samba-sync.md) | Samba in a container, kept in sync by exec helpers; no stored plaintext passwords | accepted |
| [0011](0011-gpu-vendor-and-unlimited-limits.md) | GPU vendor setting (`nvidia`/`amd`/`none`; `intel` in 0035) and "unlimited" CPU/RAM | accepted |
| [0012](0012-software-addons-and-per-user-images.md) | Optional software as add-on folders, installed by per-user image builds | accepted |
| [0013](0013-files-sharing-and-remote-access.md) | Files, sharing and remote access services | accepted |
| [0014](0014-admins-never-see-user-desktops.md) | Admins never see users' desktops or files | accepted |
| [0015](0015-python-3-14-and-httpx2.md) | Python 3.14, pydantic and httpx2 | accepted |
| [0016](0016-per-user-image-builds.md) | How per-user images are built and when they are stale | accepted |
| [0017](0017-metrics-and-admin-tools.md) | Metrics from cAdvisor and NVML; admin tools behind forwardAuth | accepted |
| [0018](0018-files-groups-and-quotas.md) | File manager, group folders, scratch and soft quotas | accepted |
| [0019](0019-ssh-help-logs-and-tls.md) | SSH gateway, help pages, log viewer and TLS | accepted |
| [0020](0020-desktop-readiness-and-db-recovery.md) | Desktop readiness, the desktop tab, and recovering from DuckDB fatal errors | accepted |
| [0021](0021-images-and-compose-files.md) | Published images and the production / development compose files | accepted |
| [0022](0022-electron-apps-without-chromium-sandbox.md) | Electron apps run without Chromium's sandbox | accepted |
| [0023](0023-deploy-folder-is-the-install.md) | The deploy folder is the install, with its own Makefile | accepted |
| [0024](0024-ai-model-library.md) | AI manager role and the model library | accepted |
| [0025](0025-server-status-from-node-exporter.md) | Server status from node-exporter, read only while the page is open | accepted |
| [0026](0026-model-fit-estimate-and-llmfit.md) | Model fit: our own estimate, llmfit only for ideas | accepted |
| [0027](0027-one-vllm-ai-session.md) | One AI session: a vLLM container with an OpenAI-compatible API | accepted |
| [0028](0028-open-webui-chat.md) | The AI chat: Open WebUI on its own port, signed in through OrbitalWindow | accepted |
| [0029](0029-coding-benchmarks.md) | Coding benchmarks with EvalPlus, run in isolated containers | accepted |
| [0030](0030-browser-terminal.md) | A browser terminal that keeps running: xterm.js, docker exec and tmux | accepted |
| [0031](0031-longer-default-context.md) | Longer contexts: plan for 32K, start with the longest that fits up to 64K | accepted |
| [0032](0032-user-environment-variables.md) | Users' own environment variables, per user and applied at start | accepted |
| [0033](0033-terminal-only-containers.md) | Terminal-only containers: LinuxServer base images in the desktop slot | accepted |
| [0034](0034-other-distributions.md) | Other distributions: ow-pkg over apt, apk, dnf and pacman | accepted |
| [0035](0035-intel-gpus.md) | Intel GPUs: `gpu_vendor=intel` passes only `/dev/dri` | accepted |
| [0036](0036-intel-gpu-numbers.md) | Intel GPU numbers: an exporter with only `CAP_PERFMON` | accepted |
| [0037](0037-vendor-packages-in-images.md) | GPU vendor packages in images, and GPU device groups for `abc` | accepted |
| [0038](0038-ai-session-on-intel.md) | The AI session on Intel GPUs: vLLM's XPU image, with limits on Arc A-series | accepted in part (see 0039) |
| [0039](0039-llama-cpp-engine.md) | A second AI engine: llama.cpp with GGUF models, for Intel and AMD | accepted |
| [0040](0040-vs-code-without-a-desktop.md) | VS Code without a desktop: code-server per user, on demand | accepted |
| [0041](0041-admin-lock-out-recovery.md) | Lock-out recovery: a one-off container while the service is stopped | accepted |
