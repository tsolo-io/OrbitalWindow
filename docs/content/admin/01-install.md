<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# Installing OrbitalWindow

This guide sets up OrbitalWindow on one server: Ubuntu 24.04 Server with NVIDIA GPU(s), on the
local network. For AMD GPUs see [AMD hosts](03-amd.md).

## 1. Host preparation

```bash
sudo apt update && sudo apt full-upgrade
sudo ubuntu-drivers install --gpgpu && sudo reboot      # then check: nvidia-smi
sudo apt install docker.io docker-compose-v2 make git
```

NVIDIA Container Toolkit, so containers can use the GPU:

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update && sudo apt install nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
docker run --rm --gpus all ubuntu nvidia-smi              # must list the GPU(s)
```

Ports used on the host: **80/443** (web), **445** (Windows file sharing) and **2222** (SSH
gateway). If the host already runs Samba, disable it: `sudo systemctl disable --now smbd nmbd`.

Disk: each desktop image is about 5 GB, and each user's software adds to that. Put Docker's data
(`/var/lib/docker`) and the OrbitalWindow data folder on a disk with plenty of space.

The admin account that runs OrbitalWindow needs to be in the `docker` group
(`sudo usermod -aG docker <you>`, then log in again).

## 2. Get OrbitalWindow and configure it

```bash
git clone <repository> orbitalwindow && cd orbitalwindow
cp deploy/.env.example deploy/.env && chmod 600 deploy/.env
```

Edit `deploy/.env`:

| Setting | Meaning |
|---|---|
| `OW_ROOT` | Data folder for homes, groups, shared software and state (default `/srv/orbitalwindow`). Back it up. |
| `OW_HOSTNAME` | The name (or IP) users type: `desk.example.org`. Also used for `\\host\user` and SSH. |
| `TZ` | Time zone, e.g. `Africa/Johannesburg`. |
| `OW_GPU_VENDOR` | `nvidia`, `amd` or `none`. |
| `OW_DEFAULT_DESKTOP` | LinuxServer webtop tag built on first start, e.g. `ubuntu-xfce` or `ubuntu-kde`. |
| `CONTROL_ADMIN_PASSWORD` | Password of the first admin (user `admin`). Used only while there are no users. |
| `OW_ACME_DNS_PROVIDER`, `OW_ACME_EMAIL` | Optional: real certificate, see [TLS](#4-tls-certificate). |

## 3. Start

```bash
make start      # builds and starts everything; `make status`, `make logs`, `make stop`
```

The first start pulls the images and builds the default desktop (10–20 minutes). Then open
`https://<OW_HOSTNAME>/`, log in as `admin` with `CONTROL_ADMIN_PASSWORD`, and:

1. Change the admin password (**Change my password** on the dashboard).
2. **Settings**: defaults for new users (CPU, RAM, disk quota), the host name, the GPU vendor.
3. **Desktops**: wait until the default desktop is *ready*; add others (e.g. `ubuntu-kde`).
4. **Users**: add users. Each gets a generated password, shown once.

## 4. TLS certificate

Until a domain and DNS provider are set up, Traefik uses a self-signed certificate for
`OW_HOSTNAME`, created once by the `certs` service and kept in the `ow-traefik-certs` volume. Browsers
warn the first time; after accepting it once, it stays valid (it changes only if `OW_HOSTNAME`
changes). To avoid the warning altogether, install it as trusted on the users' PCs: export it with
`docker run --rm -v ow-traefik-certs:/c alpine:3.22 cat /c/default.crt > orbitalwindow.crt`. For a real certificate the name must be in DNS (a public record pointing to the LAN address is
fine) and Traefik must be able to create DNS records:

1. In `deploy/.env`: `OW_ACME_DNS_PROVIDER=<lego provider code>` (for example `cloudflare`) and
   `OW_ACME_EMAIL=<admin e-mail>`.
2. Create `deploy/acme.env` (`chmod 600`) with that provider's credentials, e.g.
   `CF_DNS_API_TOKEN=…`. See the Traefik/lego documentation for each provider's variables.
3. `make restart`. The certificate is requested with a DNS-01 challenge; the server does not need to
   be reachable from the internet.

## 5. Shared software

MATLAB and the Python science environment are installed once into `OW_ROOT/shared` and mounted
read-only into the desktops of users who switch them on:

```bash
sudo OW_ROOT=/srv/orbitalwindow scripts/install-conda.sh      # ~3 GB
sudo OW_ROOT=/srv/orbitalwindow scripts/install-matlab.sh     # ~8 GB; no licence needed to install
```

MATLAB is a *restricted* add-on: grant it per user (**Users → Actions → Software access**). Each user
activates their own licence in MATLAB the first time.

## 6. Backups

Schedule `scripts/backup.sh` (see [Operations](02-operations.md#backup-and-restore)).
