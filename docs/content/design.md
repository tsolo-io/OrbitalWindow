<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# OrbitalWindow design

Status: **design, nothing implemented yet**. It was worked out in a proof of concept (see
[POC learnings](poc-learnings.md)). Build it in the phases below.

## Context
Research students (Windows PCs, not comfortable with a terminal) need GPU-accelerated Linux desktops (MATLAB, Python, R, …) on a nearby server, used **through a browser only**. A proof of concept showed that one LinuxServer *webtop* container per user, behind Traefik, with Samba for file access, works well. But it was managed by hand with shell scripts and `.env` files, and each desktop had its own password prompt.

**OrbitalWindow** is the web **control service** that makes this usable. It is the single entry point:
- Users log in once and see their desktop's status. They can start or stop it, choose from an admin-curated list of desktops, manage extra packages, and press **Open desktop** without being asked for a password again.
- Admins add and remove users, manage the desktop image list, set CPU/RAM limits, and stop users' containers. With option for unlimited.
- Samba stays in sync whenever users or passwords change.
- cAdvisor is added for container metrics.

Stack: Python, FastAPI + NiceGUI, DuckDB. It needs Docker access. Typical deployment: one server (Ubuntu 24.04, NVIDIA GPU(s), some AMD or Intel), 2–3 users, LAN only.

Further services:
- a per-user web file manager
- an SSH/SFTP gateway
- group/project folders and a shared scratch folder
- disk usage with soft quotas
- built-in help pages
- a Dozzle log viewer for admins
- real TLS certificates for a domain

### Decisions made with the user
| Topic | Decision |
|---|---|
| Container lifecycle | Docker SDK; DuckDB is the source of truth. **Start** creates a fresh container. **Stop** removes the container; the home folder stays. No per-user compose files. |
| Packages | Any Ubuntu package, checked against the apt package index of the chosen image |
| Package install | When the list or desktop choice changes, build a **per-user image** `ow-user-<name>` (chosen image + apt install). Starting is then fast. |
| Image list | Admin enters a LinuxServer webtop base tag (e.g. `ubuntu-kde`). The service builds our customised image from `deploy/image/Dockerfile`. |
| Admin access to desktops | **None.** Admins manage containers and settings, but never see a user's screen or files through the UI. |
| Metrics | Users see their own CPU/RAM plus total GPU load and memory. Admins see everything plus the cAdvisor UI. |
| Admin per-user knobs | CPU and RAM limits |
| Optional software | **Add-ons** defined as files in `addons/<key>/` (easy to add and improve, versioned in git). First set: MATLAB, Python science (conda), VS Code, Dev tools, RStudio. Admin grants *restricted* add-ons (e.g. MATLAB) per user; users switch on any open or granted add-on themselves. |
| Web file manager | **FileBrowser Quantum**, one small container per user, running as that user's UID, behind the same login. The original File Browser was archived on 2026-09-01. |
| SSH / SFTP | Opt-in `ssh` add-on (sshd in the desktop) plus one **sshpiper** gateway on host port 2222 that routes `ssh <user>@server` by container labels. Users manage their public keys in the OrbitalWindow UI. Only available while the desktop is running. |
| Sharing between users | **Group/project folders**, managed by the admin, and a **shared scratch folder**, auto-cleaned after N days (default 30). |
| Disk | Usage per user shown on the dashboard. **Soft quota**: warning at 90%, plus an admin overview. No enforcement. |
| Help | User guide served by OrbitalWindow, with server, username, Samba path and SSH command filled in |
| Admin extras | **Dozzle** log viewer, behind admin login |
| TLS | Domain name + Let's Encrypt via Traefik DNS-01. DNS provider not known yet (open point). |
| Orchestration | Plain Docker Engine, **not Swarm** (see POC learnings). Multi-server later = the service manages several Docker hosts. |

## Architecture
```
                 https://desk.<domain>  (Let's Encrypt via DNS-01)
Browser ──443──► Traefik ─┬─ /                  ─► orbitalwindow (FastAPI + NiceGUI) ── DuckDB file
                          ├─ /desktop/<user>/   ─► forwardAuth(user) ─► ow-<user>:3000
                          ├─ /files/<user>/     ─► forwardAuth(user) ─► ow-files-<user> (FileBrowser Quantum)
                          ├─ /admin/cadvisor/   ─► forwardAuth(admin) ─► cadvisor:8080
                          └─ /admin/logs/       ─► forwardAuth(admin) ─► dozzle:8080
Windows Explorer ─445───► samba     \\desk.<domain>\<user>, \\…\<group>, \\…\scratch
ssh/sftp ───────2222────► sshpiper ─► ow-<user>:2222 (only if the ssh add-on is on and the desktop runs)

orbitalwindow ──► socket-proxy-rw ──► docker.sock   (containers, exec, images, build, networks)
traefik, sshpiper, dozzle ──► socket-proxy-ro ──► docker.sock   (list/inspect containers, logs)

Per user: network ow-net-<user> joined by ow-<user>, ow-files-<user>, and (attached by OrbitalWindow) traefik + sshpiper.
```

### Single sign-on into the desktop
- At login, OrbitalWindow sets a signed session cookie `ow_session` (itsdangerous; HttpOnly, Secure, SameSite=Lax; 12-hour lifetime). It holds the user id and a session version.
- Each desktop router has a Traefik **forwardAuth** middleware pointing at `http://orbitalwindow:8000/auth/desktop`. OrbitalWindow checks the cookie and that the user named in the session owns `/desktop/<user>/` (`X-Forwarded-Uri`). It returns 200 if so, otherwise a 302 to the login page. Admins are **not** let in, per the decision above. WebSocket upgrades also pass through forwardAuth.
- Desktops no longer use the user's password. To stop anyone bypassing Traefik, OrbitalWindow generates a random **internal secret** at every start. The desktop gets it as `PASSWORD`, so nginx basic auth stays on, and a Traefik `headers` middleware on that router injects `Authorization: Basic …`. Only processes with Docker access can see the secret.
- **Per-user network** `ow-net-<user>` (an ordinary bridge with internet access). OrbitalWindow connects Traefik to it on start and disconnects on stop. Desktops can no longer reach each other, which so one user can never reach another user's desktop directly.
- Changing a password, or an admin disabling a user, bumps the session version, which signs out existing cookies.

### Desktop container spec (built by OrbitalWindow)
- **Name, image, network:** name `ow-<user>`; image `ow-user-<user>:latest`, or the chosen `ow-desktop:<tag>` if the user has no extra packages; hostname `ow-<user>`; endpoint on `ow-net-<user>` with the user's fixed **MAC address** (MATLAB host ID).
- **GPU:** `/dev/dri` passed in; `device_requests` for all NVIDIA GPUs (or `/dev/kfd` when the setting `gpu_vendor=amd`; only `/dev/dri` when it is `intel`).
- **Limits:** `cap_drop NET_RAW`, `shm_size 2g`, `nano_cpus` and `mem_limit` from the user's row.
- **Environment:** PUID/PGID = `uid_gid`, `CUSTOM_USER`, `PASSWORD=<internal secret>`, `SUBFOLDER=/desktop/<user>/`, `PIXELFLUX_WAYLAND=false`, `START_DOCKER=false`, TZ, TITLE.
- **Mounts:** `$OW_ROOT/users/<user>:/config`, plus `$OW_ROOT/groups/<g>:/groups/<g>` for each group the user belongs to, `$OW_ROOT/shared/scratch:/scratch` (rw), `$OW_ROOT/shared/data:/shared` (ro), and the mounts of selected add-ons (e.g. matlab, conda).
- **Groups:** env `OW_GROUPS="labx:3001 …"`. The init hook creates those groups in the container and adds `abc` to them, so desktop apps can write to group folders.
- **Labels:** `ow.managed=true`, `ow.user=<user>`, and the Traefik router, forwardAuth, headers and service port 3000.
- **Restart policy:** `unless-stopped`.

### Samba integration
- Samba image in `deploy/samba/` (Alpine + samba):
  - OrbitalWindow writes `$OW_ROOT/state/samba/users.json` (name, uid only; no passwords), which is mounted into Samba.
  - `docker exec ow-samba ow-sync` creates the Unix users and shares and reloads Samba.
  - `docker exec -i ow-samba ow-passwd <user>` (password on stdin) runs `smbpasswd`.
- Persist `/var/lib/samba` in a volume so passwords survive a Samba restart. That means no plaintext passwords need storing anywhere.
- Triggered by: adding a user (sync + passwd), changing or resetting a password (passwd), removing a user (`smbpasswd -x` + sync).

### Data model (DuckDB `$OW_ROOT/state/orbitalwindow.duckdb`, single writer = the service process)
- `users(id, name, display_name, pw_hash argon2, is_admin, disabled, session_ver, uid_gid, mac, image_id, cpus, mem_gb, created_at)`
- `images(id, label, base_tag, image_ref, status[building|ready|failed], built_at, enabled)`
- `pkg_index(image_id, name)`: package names from `apt-cache pkgnames` in each built image, used for autocomplete and validation
- `user_packages(user_id, name)`; `user_image(user_id, image_ref, built_from_image_id, pkg_hash, status, built_at)`
- `builds(id, kind[base|user], target, status, log, started_at, finished_at)`
- `audit(ts, actor, action, target, detail)`; `settings(key, value)`: defaults for cpus/mem/quota, gpu_vendor, public hostname, scratch retention days
- `groups(id, name, gid, description, created_at)`, `group_members(group_id, user_id)`. GID 3000+N, never reused.
- `ssh_keys(id, user_id, label, pubkey, fingerprint, added_at)`. The per-user upstream keypair (sshpiper → desktop) is generated by OrbitalWindow and kept in `$OW_ROOT/state/ssh/<user>{,.pub}`, mode 600.
- `disk_usage(subject[user|group|scratch], name, bytes, measured_at)`. Quota fields: `users.quota_gb`, `groups.quota_gb`.
- MAC and UID allocation keep the current scheme: UID 2000+N, MAC `02:42:de:5c:xx:yy`. They are never reused; the next number comes from `max`, including deleted users.

### Add-ons (optional software bundles)
Each add-on is a folder `addons/<key>/` (mounted read-only into OrbitalWindow) containing:
```yaml
# addons/matlab/addon.yaml
key: matlab
label: MATLAB
description: MATLAB R2026b (shared install). Activate your own licence on first start.
restricted: true            # admin must grant it to a user before they can switch it on
apt: [libxt6t64, libnss3, libasound2t64, ...]   # installed into the per-user image
script: install.sh          # optional root script run during the per-user image build (repos, .deb)
launchers: [ow-matlab.desktop]   # files in this folder, copied to /usr/share/applications
mounts:                     # applied when the container is created (no rebuild needed)
  - {source: shared/matlab, target: /opt/matlab, read_only: true}   # source relative to OW_ROOT
env: {}                     # extra container environment
```
- **First add-ons:**
  - `matlab`: MATLAB runtime libraries, `/opt/matlab` mount, launcher. Restricted.
  - `python-science`: `/opt/conda` mount, JupyterLab and Spyder launchers, conda bashrc hook.
  - `vscode`: script adds the Microsoft repo and installs `code`, starts it with `--no-sandbox` (ADR 0022), launcher.
  - `devtools`: build-essential, cmake, gfortran, pkg-config, git-gui, gitk, git-lfs.
  - `rstudio`: r-base from apt plus the RStudio Desktop `.deb` from Posit via script, launcher. Posit may not publish a build for Ubuntu 26.04 yet; check during implementation and fall back to the newest Ubuntu build.
- **Slim base:** `deploy/image/Dockerfile` keeps only what every desktop needs: git, python3 + venv, curl/unzip/rsync, nano, htop, nvtop (capability fix), mesa-utils, vulkan-tools, fonts. Everything above moves into add-ons.
- **Desktop icons:** the init hook (`deploy/image/root/custom-cont-init.d/50-ow-setup`) is generalised. It puts every `ow-*.desktop` from `/usr/share/applications` on the user's Desktop, and removes icons from `~/Desktop` whose add-on is gone.
- **Validation:** OrbitalWindow loads and validates all add-on definitions at startup, and when an admin presses **Reload add-ons**. Invalid definitions are shown with their error and skipped.
- **DB:** `addon_settings(key, enabled, restricted_override)`, `addon_grants(user_id, key)`, `user_addons(user_id, key)`.
- **Effect of a change:**
  - Add-ons with `apt`, `script` or `launchers` are part of the per-user image, so changing one triggers a rebuild.
  - `mounts` and `env` take effect at the next start.
  - Revoking a grant switches the add-on off for that user, and stops their container if it uses a mount from that add-on.

### Image builds
- **Base:** docker build of `deploy/image/` (mounted read-only into OrbitalWindow) with `BASE_IMAGE=lscr.io/linuxserver/webtop:<tag>`, tagged `ow-desktop:<tag>`. Afterwards, run `apt-cache pkgnames` in it once to fill `pkg_index`.
- **Per-user:** a generated build context, `FROM ow-desktop:<tag>`, then:
  - for each selected add-on, in a stable order: COPY its folder, `apt-get install` its `apt` list, run its `script`, install its `launchers`;
  - finally `apt-get install` the user's own packages;
  - clean the apt lists.

  Tagged `ow-user-<name>:latest`. It is rebuilt when the desktop, the add-on selection, an add-on's files, the package list or the base image changes (hash of all of these).
- A failed build keeps the last good image, and its log is shown in the UI.
- Builds run in a background worker with a queue, one at a time. Progress streams to NiceGUI through the `builds.log` row.

### Metrics
- **cAdvisor** (pin the v0.60.x release; check the registry when implementing). OrbitalWindow reads `/api/v2.0/stats` for containers labelled `ow.managed` to get CPU and RAM.
- **GPU** totals come from NVML (`nvidia-ml-py`). The OrbitalWindow container gets only the GPU `utility` capability. On Intel they come from `ow-gpu-stats`, a small exporter with `CAP_PERFMON` (ADR 0036). On AMD, these numbers are hidden.
- Admins get a link to the cAdvisor UI at `/admin/cadvisor/`, behind forwardAuth that checks for admin.

### Web file manager (FileBrowser Quantum, one container per user)
- **Container `ow-files-<user>`:**
  - Image `gtstef/filebrowser` (pin a version when implementing; v2 is in beta).
  - Runs as the user's UID/GID, so files it creates have the right owner. Small, and runs whether or not the desktop does.
  - Mounts the same folders as the desktop: home, groups, scratch, `/shared` read-only.
  - Configured by a `config.yaml` that OrbitalWindow generates. It uses **proxy-header auth**: Traefik's forwardAuth returns `X-OW-User`, passed on via `authResponseHeaders`. One source per mount; home is the default.
- **Lifecycle:** created with the user, stopped while the user is disabled, removed when the user is deleted. Group changes recreate it, which takes about 1 second.
- **Used for:** browse, upload and download large files in the browser, zip, preview. Share links are turned off; users share files through group folders and scratch.
- **Fallback if Quantum proves unsuitable:** a minimal upload/download page in OrbitalWindow.

### Sharing files between users
- **Group/project folders** (admin managed):
  - Location: `$OW_ROOT/groups/<name>`, owner `root:<gid>`, mode `2770` (setgid), with a default ACL `g::rwX` so files members create stay group-writable.
  - Appear as `/groups/<name>` in desktops, the file manager and SFTP, and as `\\server\<name>` in Samba (`valid users = @<name>`, `force group`, masks 0664/2775).
  - When membership changes, OrbitalWindow updates Samba straight away. Running desktops get a notice: "Restart your desktop to see the new group".
- **Shared scratch:** `$OW_ROOT/shared/scratch`, mode `1777` (sticky: everyone can add files, only the owner can delete them).
  - Available as `/scratch`, in the file manager, and as `\\server\scratch`.
  - Files untouched for N days (setting, default 30) are deleted nightly by a throwaway container. A `README-auto-clean.txt` file in the folder explains this.
- **Read-only datasets** stay in `/shared`, maintained by the admin.

### SSH / SFTP
- **The `ssh` add-on** (open, off by default):
  - Installs `openssh-server` and adds an s6 service running sshd on port 2222 inside the desktop.
  - sshd settings: `PasswordAuthentication no`, `AllowUsers abc`, `AuthorizedKeysFile /etc/ow/upstream.pub` (root-owned, injected at start), plus `Subsystem sftp internal-sftp`.
- **Gateway: one `sshpiper` container** (docker plugin) publishing host port **2222**.
  - On each desktop with the add-on on, OrbitalWindow sets these labels: `sshpiper.username=<user>`, `sshpiper.container_username=abc`, `sshpiper.port=2222`, `sshpiper.authorized_keys=<base64 of the user's keys>`, `sshpiper.private_key=<base64 upstream key>`.
  - OrbitalWindow connects sshpiper to `ow-net-<user>` like it does Traefik.
  - Exec mode (`docker_exec_cmd`) is **not** used, because it would give root shells.
- **Users:** paste public keys on the dashboard. The help page shows `ssh -p 2222 <user>@desk.<domain>`, plus WinSCP and VS Code Remote-SSH settings.
- **Keys:** changing keys updates the labels, which requires recreating the container. The UI says "takes effect at next start", or offers to restart now.

### Disk usage and soft quotas
- **Measurement:** every hour (setting), OrbitalWindow runs a throwaway container with `du -sx` per user home, group folder and scratch, and writes the results to `disk_usage`.
- **Shown:**
  - The dashboard shows "12.3 of 50 GB" with a warning at ≥90% and a hint to empty `~/proot-apps`, `.conda/pkgs` and the Trash.
  - The admin system page lists everyone, sorted by usage, plus free space on the host filesystem, with a warning when the filesystem is ≥85% full.
- **Enforcement:** none (soft quota). Quotas come from `users.quota_gb` (default from settings) and `groups.quota_gb`.

### Built-in help pages
- **`/help`** renders the markdown guides from `docs/content/user/*.md`. Placeholders such as `{host}`, `{user}`, `{samba_path}`, `{ssh_cmd}` and `{groups}` are filled in for the logged-in user.
- **Context links:** the "?" icons on dashboard cards link to the matching help section.

### Admin extras
- **Dozzle** (pinned version): reads logs through `socket-proxy-ro` and is served at `/admin/logs/` behind admin forwardAuth (Dozzle's `forward-proxy` auth mode with the `Remote-User` header).

### TLS with a domain
- **Name:** `desk.<domain>` resolves to the server's LAN IP (a public record pointing to a private IP is fine, or split DNS).
- **Certificate:** Traefik certresolver `le` with a DNS-01 challenge, so the server never needs to be reachable from the internet. The provider token lives in `deploy/acme.env`; setting `OW_ACME_DNS_PROVIDER` in `deploy/.env` adds `compose.tls.yaml` (ADR 0019).
- **Until the provider is known:** keep Traefik's self-signed default. DNS-01 is built but untested, since no domain exists yet. Route53 and a Cloudflare zone per research station are both being considered.
- **Remote access** (from home, only where a university approves) is an open question and not built. Options discussed on 2026-10-11: Cloudflare Tunnel with Access, Microsoft Entra Application Proxy, Tailscale or NetBird, or a self-hosted relay on AWS (Pangolin, or WireGuard with Traefik). Whichever is chosen, OrbitalWindow's own login stays on for LAN users.
- **Samba and SSH** use the same hostname (`\\desk.<domain>\<user>`, `ssh -p 2222 <user>@desk.<domain>`).

### AI models (ticket #15, ADR 0024)
- **Role:** `users.is_ai_manager` (admins count too); page `/ai`.
- **Library:** `$OW_ROOT/shared/models/<org>/<name>`, one plain folder per model
  (`hf download --local-dir`), owned by the service UID.
- **Downloads:** one at a time in `ow-model-dl` (image `ow-hf:1`), pinned to the inspected commit:
  the safetensors weights, or one chosen GGUF file (its parts) for llama.cpp (#54). Progress, cancel and resume are on the AI page.
- **Token:** `$OW_ROOT/state/hf_token` for gated models.
- **Fit (#16, ADR 0026):** estimate from weights, KV cache and overhead; llmfit image for
  suggestions.
- **Session (#17, ADR 0027):** one vLLM container `ow-ai-model` on internal network `ow-ai`, with an
  API key from `state/ai_api_key`. `/v1` goes through Traefik only when switched on; it is in the
  `ai_sessions` history. vLLM gets a tool-call parser and, for Qwen3 thinking models, a reasoning
  parser chosen from `config.json` and the chat template (#30, #31); `--max-num-seqs 16` lets
  hybrid models such as Qwen3.5 start. The fit counts only full-attention layers for the KV cache.
- **Engine (#54, ADR 0039):** the setting `ai_engine` is `vllm` or `llamacpp`. llama.cpp runs a GGUF
  file in `ghcr.io/ggml-org/llama.cpp:server-<cuda|intel|rocm>-<build>` with the same container,
  network, port and key, so the chat, the API and benchmarks don't depend on it. Intel hosts use
  llama.cpp: vLLM hangs the Arc A770 (ADR 0038).
- **Context (#38, ADR 0031):** the fit check plans for 32,768 tokens, the least coding tools such
  as OpenCode need. A session starts with the longest context that fits, up to 65,536 tokens and
  the model's maximum (benchmarks use 8,192). The dashboard's AI card shows every user the running
  context.
- **Chat (#18, ADR 0028):** Open WebUI `ow-openwebui` on port 8443 (Traefik entrypoint `ai`)
  while the session runs. It signs users in from the forwardAuth headers `X-OW-Email`/`Name`/`Role`
  and offers per-user API keys (`Bearer sk-…` skips forwardAuth). Its data is in `state/openwebui`.
- **Benchmarks (#21, ADR 0029):** EvalPlus HumanEval+/MBPP+. `ow-bench-generate` runs on `ow-ai`;
  `ow-bench-evaluate` runs with no network. Files are in `state/benchmarks/<id>/` with a
  `benchmarks` row. Pages `/benchmarks` and `/benchmarks/<id>` are for every user.

### Server status (ticket #22, ADR 0025)
- Page `/status` for every user: host name, uptime, load, CPU, memory, swap, disk space, disk and
  network traffic, GPUs (NVML), and temperatures (#33): hwmon chips from node-exporter averaged per
  chip with the hottest sensor and critical limit, GPU temperature and slowdown limit from NVML.
- **Source:** Prometheus node-exporter on the host (admin installs it), at `OW_NODE_EXPORTER_URL`
  (default `http://host.docker.internal:9100/metrics`, via `extra_hosts: host-gateway`).
- Read only by an open page, every 5 s; rates come from the page's previous read. Nothing is
  cached or polled in the background. Without node-exporter, the page shows what to install.

### Browser terminal (ticket #39, ADR 0030)
- Page `/terminal` (own desktop only): NiceGUI `ui.xterm` attached through a TTY `docker exec`
  (`socket-proxy-rw`), as `abc` in `/config`.
- The shell runs in a tmux session `ow` in the desktop. Closing the page detaches it, so programs
  keep running. **Stop terminal** (page or dashboard) ends the session. Removing a running desktop
  ends the session first and waits 2 s, and the dashboard asks before stopping one whose terminal
  runs.
- tmux is in the desktop base image. Older desktops get a plain shell that ends with the page.

### Terminal-only containers (ticket #40, ADR 0033)
- An image row with `gui = false` is built from a LinuxServer base image (`base_image`, for
  example `lscr.io/linuxserver/baseimage-ubuntu:noble`) with `deploy/image/Dockerfile.terminal`,
  as `ow-terminal:<name>`. It sits in the same slot as a desktop: one container per user.
- The container has the same resources but no Traefik route or desktop settings. It is used
  through **Open terminal** and SSH, and SSH is switched on when it is chosen. Add-ons with
  `gui: true` are not offered.

### Other distributions (ticket #40, ADR 0034)
- `ow-pkg` (in every image) wraps apt, apk, dnf and pacman. `base-packages.sh` installs the base
  tools with each distribution's names, and `images.pkg_manager` is recorded with the package
  index.
- Add-ons list packages per manager (`apt`, `apk`, `dnf`, `pacman`, optional `managers`). Those not
  for the user's system are greyed out and left out of the image.

### Environment variables (ticket #41, ADR 0032)
- Page `/environment` (linked from the desktop card): the user's own variables (`user_env`), set
  in every desktop they start, whichever desktop it is. They win over add-on `env`. Names
  OrbitalWindow or the start-up relies on are refused.
- They apply at the next start: the `ow.env` label hash tells the page when a restart is needed. The
  init hook writes them to `/etc/profile.d/ow-user-env.sh` for SSH logins. The audit log records
  names only.

### UI pages (NiceGUI)
- **`/login`**
- **`/` (user dashboard):**
  - Status (stopped, building, starting, running) with Start and Stop buttons, plus a desktop picker showing enabled images.
  - **Open desktop** (opens `/desktop/<user>/` in a new tab).
  - **Open terminal** (opens `/terminal` in a new tab); while a terminal runs, a note and **Stop terminal**.
  - **Environment variables** link (opens `/environment`).
  - Own CPU/RAM and GPU totals.
  - **Software** card: add-on switches (open ones, plus restricted ones the admin granted; others are greyed out with "ask admin").
  - Package editor: list, add with autocomplete, remove, **Apply**, which builds and shows the log.
  - **Files** button (opens `/files/<user>/`), disk usage against quota, list of groups with their paths.
  - **SSH keys** card (add, remove, fingerprints; how to connect), shown when the ssh add-on is on.
  - Change password. Samba paths shown (`\\desk.<domain>\<user>`, groups, scratch). **Help** link.
- **`/admin/users`:** table of users (status, desktop, container state); actions as in *Account management*: create, set/reset password, disable/enable, delete, stop container, CPU/RAM, add-on grants.
- **`/admin/images`:** add a base tag (starts a build), enable/disable, rebuild, remove (only if unused), build logs.
- **`/admin/addons`:** loaded definitions with validation status, enable/disable, restricted on/off, **Reload add-ons**. Per-user grants are set on `/admin/users`.
- **`/admin/groups`:** create/delete groups (deleting keeps the folder unless confirmed), members, quota.
- **`/admin/settings`:** default CPU/RAM/quota, scratch retention, hostname, GPU vendor.
- **`/admin/system`:** all desktop and file-manager containers with status, CPU, RAM, image; GPU; disk usage table and host free space; links to cAdvisor and Logs (Dozzle); audit log.

### Account management (exact effects)
Every action is recorded in `audit`.

| Action | Who | Effects |
|---|---|---|
| **Change own password** | User (dashboard) | Asks for the current password, checks the minimum length, stores the argon2 hash, runs `ow-passwd` in Samba, and bumps `session_ver` (other sessions are signed out; the current one is re-issued). |
| **Set or reset a user's password** | Admin | Either enter a new password or generate one, which is shown once. Same Samba update and session bump. The running container is not affected. |
| **Create user** | Admin | Name (validated, not reserved), display name, admin flag, desktop, CPU/RAM (defaults from settings), add-on grants. Allocates UID and MAC, creates the home folder, Samba user and share. Shows the initial password once. |
| **Disable / enable** | Admin | Disable: blocks login, desktop and file manager access (session bump), stops and removes the desktop (which ends SSH), stops `ow-files-<user>`, disables the Samba account (`smbpasswd -d`). Enable reverses all of these except the desktop, which the user starts again themselves. |
| **Delete user** | Admin (confirm by typing the name) | Stops and removes the desktop, `ow-files-<user>`, network and per-user image; removes the Samba user and share, group memberships, SSH keys and the upstream keypair; removes the DB rows except `audit`. The **home folder is kept by default** and its path is shown. An extra "also delete home folder" checkbox removes it, using a throwaway container because files are owned by the user's UID. UID and MAC are never reused. An admin cannot delete or disable themselves or the last admin. |
| **Stop a user's container** | Admin | Stops and removes the container (home folder kept). The user's dashboard shows "stopped by admin". |

### Background reconcile loop (every 30 s and at startup)
- List `ow.managed` containers and update the status shown.
- Make sure Traefik and sshpiper are connected to every user network that has a running container (connections are lost if they are recreated).
- Make sure every enabled user has a running `ow-files-<user>`.
- Remove networks of deleted users.
- Scheduled jobs: disk usage (hourly), scratch clean-up (nightly).

### Security notes
- Docker access is root-equivalent. The proxies limit the API calls OrbitalWindow and Traefik can make, but OrbitalWindow is still effectively root, so it gets:
  - argon2 password hashes
  - a login rate limit
  - first admin created from `CONTROL_ADMIN_PASSWORD` on first start
  - an audit log
  - no Docker features beyond what the design needs.
- The session secret lives in `$OW_ROOT/state/secret.key`, mode 600. LAN only, as before.

## Repository layout
```
pyproject.toml            hatch project (envs: default, hatch-test, lint, docs)
src/orbitalwindow/        the service
  main.py                 FastAPI app with NiceGUI mounted; /auth/desktop, /auth/files, /auth/admin (forwardAuth)
  config.py db.py auth.py desktops.py builds.py addons.py samba.py files.py groups.py
  ssh.py disk.py metrics.py reconcile.py help.py
  ui/                     login, dashboard, help, admin_{users,images,addons,groups,settings,system}
tests/                    pytest (fake Docker client for container-spec tests)
deploy/
  compose.yaml            traefik, orbitalwindow, socket-proxy-rw/-ro, samba, cadvisor, sshpiper, dozzle
  .env.example            OW_ROOT, TZ, hostname, CONTROL_ADMIN_PASSWORD, DNS token
  Dockerfile              image for the service (python:3.14-slim)
  image/                  desktop base image: Dockerfile + root/ (custom-cont-init.d/50-ow-setup)
  samba/                  Samba image: Dockerfile, smb.conf, entrypoint, ow-sync, ow-passwd
  Makefile                start/stop/update, install-conda/-matlab, backup (what a server runs)
  scripts/                install-conda.sh, install-matlab.sh (shared software), backup.sh
addons/                   add-on definitions (README.md documents the format)
  matlab/ python-science/ vscode/ devtools/ rstudio/ ssh/
docs/                     zensical.toml + content/ (design.md, poc-learnings.md, admin guide, user/ help pages)
```
- **Names:** containers `ow-<user>` (desktop) and `ow-files-<user>`, network `ow-net-<user>`, images `ow-desktop:<tag>` and `ow-user-<name>:latest`, labels `ow.managed=true` and `ow.user=<user>`, cookie `ow_session`.
- **Host data root `$OW_ROOT`** (default `/srv/orbitalwindow`): `users/<name>/` (homes), `groups/<name>/`, `shared/{matlab,conda,data,scratch}/`, `state/` (DuckDB, secret, ssh keys, samba). Back up `users/`, `groups/` and `state/`.
- **Versions to start from** (check for newer ones):
  - Python: fastapi 0.143, nicegui 3.18, duckdb 1.5, docker 7.2, argon2-cffi 25.1, itsdangerous 2.2, httpx 0.28, nvidia-ml-py, pyyaml.
  - Containers: traefik v3.7.14, tecnativa/docker-socket-proxy v0.5.0, cAdvisor v0.60.x, alpine:3.22 (samba 4.21), base image `lscr.io/linuxserver/webtop:ubuntu-{xfce,kde}`.

## Implementation phases
1. **Core and SSO:** hatch project, deploy stack (Traefik, socket proxies, Samba), service skeleton (DB, first admin, login, session, `/auth/desktop`), desktop image, container spec, start/stop with per-user networks. Verify that **Open desktop** needs no second password.
2. **Images, add-ons and packages:** slim base image, add-on loader and the five add-ons, base-image builds with `pkg_index`, per-user image builds, software card and package editor, build logs.
3. **Admin and metrics:** account management actions, admin pages (users, images, add-ons, system), cAdvisor + NVML, audit log, reconcile loop.
4. **Files and sharing:** per-user FileBrowser Quantum, group folders and scratch (desktop, Samba, file manager), disk usage and soft quotas, clean-up job.
5. **Access and polish:** ssh add-on + sshpiper + key management, help pages, Dozzle, TLS via DNS-01 once the provider is known.
6. **Document and harden:** admin guide (production install, AMD hosts), backup script, review of the security notes.

## Verification
- **pytest:** auth and forwardAuth matrix (no cookie → 302; user1's cookie on `/desktop/user2/` → 403; admin cookie on a user desktop → 403; valid → 200), container spec snapshot, allocation.
- **End to end on the test server:**
  - Log in as user1. Start the desktop, then **Open desktop**: the stream loads with no basic-auth prompt.
  - `curl` to `/desktop/user1/` without the cookie → redirect to login.
  - From inside `ow-user1`, `ow-user2:3000` is unreachable.
  - Stop the desktop: the container and network are gone, and the home folder is intact.
- **Packages:** add `octave`, Apply, see the build log, start the desktop, `octave --version` works. A non-existent package is rejected.
- **Add-ons:** user3 without the MATLAB grant sees it greyed out. After the admin grants it and user3 switches it on and applies, the MATLAB icon appears and `/opt/matlab` is mounted. Switching VS Code off removes `code` and its icon after the rebuild. A broken `addon.yaml` shows an error on `/admin/addons` and is skipped.
- **Accounts:** a user changes their own password → the old one fails for both login and Samba, and the new one works. Admin disables user3 → login is refused, the container is stopped, Samba is refused; enable restores access. Admin deletes user4 → container, image and Samba share are gone, the home folder is kept. Deleting the last admin is refused.
- **Admin:** create user4 → the Samba share works with the new password. Change the password → the old Samba password fails and the new one works. Stop user1's container from the admin page. Add base tag `ubuntu-mate` → it builds and appears in users' pickers.
- **Restarts:** after restarting Samba, passwords still work. After recreating Traefik, the reconcile loop reconnects networks within 30 s.
- **Metrics:** the dashboard shows own CPU/RAM and GPU totals. `/admin/cadvisor/` works for an admin and returns 403 for a user.
- **MAC/host ID:** each desktop's `eth0` has the user's fixed MAC across stop/start and rebuilds.
- **File manager:** **Files** opens without a password prompt. A file uploaded there appears in the desktop owned by the user's UID. `/files/user2/` with user1's cookie → 403.
- **Groups:** admin creates `labx` with user1 and user3. A file user1 creates in `/groups/labx` (desktop, Samba or file manager) can be edited by user3. user2 can't see it anywhere. `\\server\labx` works for members only.
- **Scratch:** user1 writes to `/scratch`; user2 can read it but not delete it. A file back-dated beyond N days is removed by the clean-up job.
- **SSH:** user1 turns on the ssh add-on and adds a key. `ssh -p 2222 user1@host` gives a shell as `abc` in their desktop, and `sftp` works. Using user2's name with user1's key is refused. With the desktop stopped, the connection is refused cleanly.
- **Disk:** the dashboard shows usage. A user over 90% sees a warning. The admin page lists all users and the host's free space.
- **Logs:** `/admin/logs/` works for an admin and returns 403 for a user.
- **TLS (once configured):** the browser shows a valid certificate for `desk.<domain>` with no warning.

## Open points
- **DNS provider** for Let's Encrypt DNS-01: unknown. Until then, self-signed.
- **FileBrowser Quantum v2 is in beta:** pin a tested version. Fallback: a minimal upload/download page in OrbitalWindow.
- **Group membership in the desktop:** check that s6 (the webtop init system) keeps the supplementary groups the init hook adds to `abc`. If not, set the groups with `group_add` on the container.
- **RStudio on Ubuntu 26.04:** check that Posit's `.deb` installs.
- **Auto-stop of idle desktops:** out of scope. It can be added later in the reconcile loop.
- **DuckDB** is single-writer. Fine for one process; moving to another database only touches `db.py`.
