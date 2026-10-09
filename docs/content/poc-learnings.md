<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# What the proof of concept taught us

A hand-built proof of concept (POC) ran on a test server in October 2026. This page records what we
verified, what broke, and why the [design](design.md) looks the way it does. Nothing from the POC is
carried over as code. Treat the snippets below as reference, not as files to copy.

## Goals and users

- Research students on **Windows**, mostly not comfortable with a terminal. They need GPU-accelerated
  Linux desktops (MATLAB, Python, R, VS Code, …) on a server nearby that has no screen or keyboard.
- **Browser only**, authenticated, each user's data separate. **2–3 users per server**, LAN only.
- Production: **Ubuntu 24.04 Server**, mostly NVIDIA GPUs, some AMD.

## Why containers rather than Ubuntu Desktop + remote access software

| | Ubuntu Desktop + xrdp/GNOME RDP/AnyDesk | One webtop container per user |
|---|---|---|
| Users at the same time | One console session. xrdp gives more, but with poor GPU/3D | Each user has their own desktop |
| Browser only | No (needs an RDP client, or Guacamole without GPU encoding) | Yes: Selkies WebRTC/WebSocket with NVENC |
| Isolation / recovery | Shared OS, so one user can break it for everyone | Recreate a container in seconds; the home folder survives |
| sudo for users | sudo on the real host | sudo only inside their own container |

Also rejected:

- **Kasm Workspaces:** good, but licence limits and less control.
- **LinuxServer SealSkin:** needs a browser extension and RSA key files instead of passwords, mounts
  the Docker socket, and is meant to face the internet. Its NVIDIA support needs the proprietary module.
- **Docker Swarm:** no benefit on one host, and it breaks needed features. Swarm services
  can't do `--gpus`/`devices` the same way or set a fixed per-endpoint MAC, and locally built images
  would need a registry. If we ever need several servers, OrbitalWindow should manage several plain
  Docker hosts instead.

## Test server

- **OS and runtime:** Ubuntu **26.04**, kernel 7.0, Docker 29.1 (API 1.52), nvidia-container-toolkit 1.20.
- **Hardware:** **RTX 5080 16 GB**, driver **595.91**, using the **open kernel module** ("Dual MIT/GPL").
  RTX 50xx (Blackwell) supports only the open module. 6 CPUs, 60 GB RAM.
- **Disk:** tight (87% used after MATLAB and two desktop images). Plan disk space carefully.
- **Check:** `docker run --rm --gpus all ubuntu nvidia-smi` worked out of the box.

## The webtop image (LinuxServer)

- **Image:** `lscr.io/linuxserver/webtop:ubuntu-xfce` / `:ubuntu-kde` (also mate, i3, …). As of 2026-10 it is
  based on **Ubuntu 26.04**. The 24.04 (noble) Selkies base image stopped being updated in June 2026,
  so use the current tags.
- **Streaming:** Selkies 2.0 with pixelflux. It detects NVENC on its own. Log line:
  `[pixelflux] Render node 0 encodes H264, AV1, H265 on nvenc.` **Three desktops streamed at once**,
  using ~1.5 GB VRAM in total.
- **GPU passthrough:** `device_requests` (`--gpus all`) + `/dev/dri` + `NVIDIA_DRIVER_CAPABILITIES=all`.
  The image sets up the NVIDIA ICD, EGL and GLX by itself. Check with
  `glxinfo -B` → `OpenGL renderer string: zink Vulkan 1.4(NVIDIA GeForce RTX 5080 ...)`.
- **Home:** `/config` is `$HOME`. At start it is chowned to `PUID:PGID`. The user inside is always `abc`
  (UID = PUID), with NOPASSWD sudo unless `DISABLE_SUDO=true`.
- **Environment variables we used:**
  - `PUID`/`PGID`, `TZ`, `TITLE`.
  - `CUSTOM_USER` + `PASSWORD`: turn on nginx **basic auth** on both 3000 and 3001
    (the init script uncomments `auth_basic` lines).
  - `SUBFOLDER=/path/`: serves the page, `/api` WebSocket, etc. under a path prefix.
  - `START_DOCKER=false`: turns off the built-in Docker-in-Docker.
  - `PIXELFLUX_WAYLAND`, `DRINODE`, `DISABLE_SUDO`.
- **Ports listening inside the container:**
  - 3000 (HTTP nginx) and 3001 (HTTPS nginx, self-signed cert in `/config/ssl`).
  - Selkies on 8082, bound to **localhost only**, plus other localhost-only ports.

  So nginx (with basic auth) is the only way in from the network.
- **Init hooks:** scripts in `/custom-cont-init.d/`, baked into the image, run as root on every start
  (`[custom-init] … executing`). We used one to put launchers on `~/Desktop` and `chmod 750 /config`.
- **Bundled software:** Chromium as the browser, Python 3.14 at `/lsiopy`, and `proot-apps`
  (see below).
- **Packages:** `apt install` inside a container is **lost** when it is recreated.

### Problems found and how we fixed them

| Problem | Cause | Fix |
|---|---|---|
| KDE rendered in software (`llvmpipe`) | The KDE image defaults to `PIXELFLUX_WAYLAND=true`. GBM allocation fails on the NVIDIA open module: `KMS: DRM_IOCTL_MODE_CREATE_DUMB failed: Permission denied` → `GPU renderer failed to initialize … Pixman` | Set `PIXELFLUX_WAYLAND=false` for every desktop. KDE then runs `kwin_x11` and renders via zink on NVIDIA |
| `nvtop`: "Operation not permitted" | Ubuntu's nvtop has file capability `cap_perfmon=ep`, which isn't in Docker's bounding set, so exec fails | `setcap -r /usr/bin/nvtop` in the image build (NVIDIA stats via NVML don't need it) |
| VS Code aborted: *SUID sandbox helper … not configured correctly* | `/usr/share/code/chrome-sandbox` must be setuid root | `chown root:root … && chmod 4755 …` in the image build. Its launcher is `com.microsoft.VSCode.desktop` |
| A shared conda env was created in a throwaway `$HOME` | `envs_dirs` in `.condarc` points to `~/.conda/envs` first | Create shared envs with `conda env create -p /opt/conda/envs/<name>` |

## Users' software

- **Shared read-only installs** save disk and time:
  - **Miniforge at `/opt/conda`**, mounted read-only, with a `science` env (numpy, scipy, pandas,
    matplotlib, scikit-learn, jupyterlab, spyder, `nb_conda_kernels`). Its `.condarc`:
    `envs_dirs: [~/.conda/envs, /opt/conda/envs]`, `pkgs_dirs: [~/.conda/pkgs]`.
    Users' own envs then go to their home, and `nb_conda_kernels` shows them in JupyterLab.
  - **Shell hook:** `/etc/bash.bashrc` sources `/opt/conda/etc/profile.d/conda.sh` if it exists.
  - **conda-forge** also covers R, Octave, gcc, cmake, ffmpeg and more without root.
- **proot-apps** (already in the image, copied to `~/.local/bin`):
  - `proot-apps install gimp` works with no root, even in our hardened container.
  - It installs into `~/proot-apps`, adds a menu entry and desktop icon, and survives recreation.
  - The catalogue (github.com/linuxserver/proot-apps) had 123 apps. It is **not** apt.
  - Each user gets their own copy (Firefox ≈ 1.6 GB).
- **Per-user apt package lists:** the design builds a per-user image instead of installing at every start.
  LinuxServer's `universal-package-install` mod was considered but never tested.

## MATLAB

- **Install** without a licence using MathWorks Package Manager:
  `mpm install --release=R2026b --destination=/opt/matlab --products MATLAB Parallel_Computing_Toolbox`
  (≈ 7.8 GB). Share it read-only to desktops.
- **On Ubuntu 26.04** (not officially supported) it loaded with **no missing libraries** using these runtime packages:
  `ca-certificates libasound2t64 libcairo-gobject2 libcairo2 libcap2 libcups2t64 libdrm2 libgbm1
  libgdk-pixbuf-2.0-0 libgl1 libglib2.0-0t64 libgstreamer-plugins-base1.0-0 libgstreamer1.0-0
  libgtk-3-0t64 libice6 libnspr4 libnss3 libodbc2 libpam0g libpango-1.0-0 libpangocairo-1.0-0
  libpangoft2-1.0-0 libsndfile1 libuuid1 libwayland-client0 libxcomposite1 libxcursor1 libxdamage1
  libxfixes3 libxft2 libxinerama1 libxrandr2 libxt6t64 libxtst6 libxxf86vm1 libxmu6 locales
  locales-all make net-tools procps`.
- **Licence search path** (seen in the error message):
  `/config/.matlab/R2026b_licenses:/opt/matlab/licenses/license.dat:…`. Each user's activation
  lives in **their home folder**, so a read-only shared install works. `MathWorksProductAuthorizer` is
  included for activation in the GUI.
- **Individual licences are node-locked** to the **MAC address of `eth0`** (plus the username, `abc`). So each user
  has a **fixed MAC** (`02:42:de:5c:xx:yy`, never reused). With more than one network, put the MAC on the
  network with the highest `priority` so it becomes `eth0`. Verified: `eth0` kept the MAC across recreation.
- Whether individual licences may be used on a shared server is unclear. Check with MathWorks.
- `gpuArray` is **NVIDIA only**. AMD GPUs give a smooth desktop but no MATLAB GPU computing.

## GPU sharing

- **Default:** every desktop sees all GPUs. Compute is time-sliced between users, and **GPU memory is not divided**:
  first come, first served, and out-of-memory errors are possible. The user accepted shared use.
- **Not available on GeForce:** MIG. Exclusive-process compute mode would break desktop rendering and NVENC.
- **Optional:** pin a GPU per user with `device_ids`. Inside a container, nvtop shows overall GPU load but only
  that user's own processes.

## Networking, routing and security

- **Traefik v3.7.14** with Docker labels worked, including WebSockets. Gotchas:
  - `PathPrefix(`/user1`)` also matches `/user10`. Use ``Path(`/x`) || PathPrefix(`/x/`)``, plus a
    `redirectregex` that adds the trailing slash.
  - In compose labels, escape `$` as `$$`.
  - With several networks, set `traefik.docker.network` (or `--providers.docker.network`).
  - An `internal: true` network can't publish ports and has no internet, so containers need another network too.
  - Docker compose `extends` does not bring over top-level `networks:`.
- **Shared proxy network risk:** a user (with sudo) could reach another desktop's nginx, which is protected only by its password,
  and with `NET_RAW` could sniff or spoof plain HTTP. We verified `cap_drop: [NET_RAW]` blocks raw sockets even under sudo.
  The design goes further: **per-user networks** and a **random internal secret** per start.
- **Docker socket access** is root-equivalent. Put socket proxies in front, and give the read-only proxy to Traefik.
- **Host home folders:** a separate UID per user (2000+N), mode 750.
- **Testing without a real browser:** headless Chromium inside a container
  (`chromium --headless=new --no-sandbox --ignore-certificate-errors --screenshot …
  https://user:pass@host/path/`) connects. The video can't be decoded there, but the logs show
  `settings applied … h264enc`, and `nvidia-smi --query-gpu=encoder.stats.sessionCount` shows the NVENC session.

## Samba (file access from Windows)

- **Alpine 3.22 + samba 4.21 in a container**, sharing each home folder as `\\server\<user>` with the same password.
  - Each Samba user must exist as a Unix user with **UID = the desktop's PUID**. Files written over Samba then have the right owner.
  - Verified with `smbclient`: own share works; another user's share → `ACCESS_DENIED`; wrong password and guest refused.
- **`smb.conf` settings that mattered:**
  ```
  server min protocol = SMB2_10
  server signing = mandatory        # Windows 11 24H2 requires signing
  server smb encrypt = desired
  map to guest = never
  restrict anonymous = 2
  disable netbios = yes
  smb ports = 445
  access based share enum = yes     # users only see their own shares
  veto files = /.*/                 # hide .config/.conda etc. so they can't be broken from Windows
  delete veto files = no
  ```
- **Passwords and ports:** the passdb is lost on restart unless `/var/lib/samba` is a volume. Windows needs port **445**; it can't be changed.

## Other tools evaluated

- **File Browser** (filebrowser/filebrowser) was **archived on 2026-09-01**. **FileBrowser Quantum**
  (`gtstef/filebrowser`) is the maintained fork. It supports proxy-header auth and multiple sources; v2 is in beta.
- **sshpiper docker plugin** routes SSH by container labels: `sshpiper.username`,
  `sshpiper.container_username`, `sshpiper.port`, `sshpiper.authorized_keys` (base64),
  `sshpiper.private_key` (base64). Avoid `sshpiper.docker_exec_cmd=true`, which execs into the container.

## Production host setup (Ubuntu 24.04)

```bash
sudo ubuntu-drivers install --gpgpu && sudo reboot          # then: nvidia-smi
sudo apt install docker.io docker-compose-v2
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update && sudo apt install nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
docker run --rm --gpus all ubuntu nvidia-smi
```

- **AMD hosts:** install the amdgpu driver (plus ROCm for compute), pass `/dev/dri` (and `/dev/kfd`), skip the NVIDIA
  toolkit. Video encoding uses VA-API. Wayland mode may work there.
- **Host Samba:** if the host already runs Samba, disable it (`systemctl disable --now smbd nmbd`) so the container can bind 445.
