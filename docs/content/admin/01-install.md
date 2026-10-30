<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# Installing OrbitalWindow

This guide sets up OrbitalWindow on one server: Ubuntu 24.04 Server with NVIDIA, AMD or Intel
GPU(s), on the local network. You prepare the server (step 1), then an install script does the rest
(step 2).

## 1. Host preparation

```bash
sudo apt update && sudo apt full-upgrade
sudo apt install docker.io docker-compose-v2 make
```

Prepare the GPU. Each vendor has its own page:

- [NVIDIA](01-install-nvidia.md): driver and NVIDIA Container Toolkit.
- [AMD](01-install-amd.md): `amdgpu` driver (and ROCm for GPU computing).
- [Intel](01-install-intel.md): kernel driver (`i915` or `xe`) only.

A test machine without a GPU needs no preparation. Use `OW_GPU_VENDOR=none`: desktops then render
in software.

Prometheus node-exporter, for the **Server status** page (OrbitalWindow reads it from the host's
port 9100 while someone has the page open; without it the page says so):

```bash
sudo apt install prometheus-node-exporter    # starts it as a service on port 9100
```

Ubuntu's package ignores filesystems mounted under `/var/lib`. If Docker's data or the data folder
is on its own disk mounted there, include it by changing `ignored-mount-points` in
`/etc/default/prometheus-node-exporter` to `^/(sys|proc|dev|run|tmp)($|/)` and restarting the
service (`sudo systemctl restart prometheus-node-exporter`).

If a firewall runs on the host, allow port 9100 from Docker's networks (or only from the
OrbitalWindow service). Don't expose it to the LAN unless you want to.

Ports used on the host: **80/443** (web), **8443** (AI chat), **445** (Windows file sharing)
and **2222** (SSH gateway). If the host already runs Samba, disable it: `sudo systemctl disable --now smbd nmbd`.

Disk: each desktop image is about 5 GB, and each user's software adds to that. Put Docker's data
(`/var/lib/docker`) and the OrbitalWindow data folder on a disk with plenty of space.

## 2. Install

Download the install script and run it:

```bash
curl -fsSLO https://github.com/tsolo-io/OrbitalWindow/releases/latest/download/install.sh
sudo bash install.sh
```

The script:

1. Checks Docker and Docker Compose. If one is missing it says how to install it and stops.
2. Looks for GPUs and checks their driver (on NVIDIA also the NVIDIA Container Toolkit). If there is
   no GPU, or its driver doesn't work yet, it says so and installs without a GPU: desktops then
   render in software.
3. Asks three questions; press Enter to take the suggestion:
    - the name or IP address users type in the browser (suggested: this server's IP address),
    - the data folder (suggested: `/srv/orbitalwindow`),
    - the time zone (suggested: the server's).
4. Checks that ports 80, 443, 445, 2222 and 8443 are free.
5. Downloads the latest release into `/opt/orbitalwindow` and writes the settings to `.env` there.
6. Adds you to the `docker` group, so the `make` commands work without `sudo` (log out and in again
   first), and installs `make` if it is missing.
7. Starts OrbitalWindow and prints the address and the first admin password.

Options, for example to install a particular version or without questions:

| Option | Meaning |
|---|---|
| `--version v1.2.0` | Install this release; `main` is the latest development build. Default: the latest release. |
| `--hostname NAME` | Name or IP address users type. |
| `--data DIR` | Data folder (`OW_ROOT`). |
| `--dir DIR` | Folder for the deploy files. Default `/opt/orbitalwindow`. |
| `--gpu VENDOR` | `nvidia`, `amd`, `intel` or `none`, instead of what the script detects. |
| `--yes` | Don't ask; take the suggestions. |

All `make` commands below run in `/opt/orbitalwindow` (`make help` lists them). Then continue with
[First login](#3-first-login).

### Changing the GPU later

If the server was installed without a GPU and the driver works now (check again with
`sudo bash install.sh`, which updates and reports what it finds):

1. In `/opt/orbitalwindow/.env`, set `OW_GPU_VENDOR` (`nvidia`, `amd` or `intel`), and set
   `COMPOSE_FILE` to `compose.yaml:compose.nvidia.yaml` (NVIDIA), `compose.yaml:compose.intel.yaml`
   (Intel) or `compose.yaml` (AMD).
2. Run `make update` in that folder.
3. In OrbitalWindow, choose the GPU under **Settings → GPU**.

### Installing by hand

Without the script: download the release's `orbitalwindow-deploy-<version>.tar.gz` (or copy the
repository's `deploy/` folder), unpack it in `/opt/orbitalwindow`, and create `.env`. Keep that folder
apart from the data folder (`OW_ROOT`), so it can be replaced on an update without touching data:

```bash
sudo mkdir -p /opt/orbitalwindow && sudo chown $USER /opt/orbitalwindow
tar -xzf orbitalwindow-deploy-<version>.tar.gz --strip-components=1 -C /opt/orbitalwindow && cd /opt/orbitalwindow
make init                     # creates .env (mode 600) and prints the first admin password
```

The admin account that runs OrbitalWindow needs to be in the `docker` group
(`sudo usermod -aG docker <you>`, then log in again). Edit `.env`:

| Setting | Meaning |
|---|---|
| `OW_VERSION` | Image version: a release tag such as `v1.0.0`, or `main` for the latest build of the main branch. |
| `OW_ROOT` | Data folder for homes, groups, shared software and state (default `/srv/orbitalwindow`). Back it up. |
| `OW_HOSTNAME` | The name (or IP) users type: `desk.example.org`. Also used for `\\host\user` and SSH. |
| `TZ` | Time zone, e.g. `Africa/Johannesburg`. The web pages show times in this zone. |
| `OW_GPU_VENDOR` | `nvidia`, `amd`, `intel` or `none` (no GPU, for testing only). |
| `OW_DEFAULT_DESKTOP` | LinuxServer webtop tag built on first start, e.g. `ubuntu-xfce` or `ubuntu-kde`. |
| `CONTROL_ADMIN_PASSWORD` | Password of the first admin (user `admin`), generated by `make init`. Used only while there are no users. |
| `COMPOSE_FILE` | Compose files for a plain `docker compose` (see below); `make` ignores it. |
| `OW_ACME_DNS_PROVIDER`, `OW_ACME_EMAIL` | Optional: real certificate, see [TLS](#4-tls-certificate). |

Then start it:

```bash
make start            # pulls the published images and starts; `make status`, `make logs`
```

`make` picks the compose files from `.env`: `compose.nvidia.yaml` when `OW_GPU_VENDOR=nvidia` or
`compose.intel.yaml` when it is `intel` (without them the admin pages show no GPU numbers) and
`compose.tls.yaml` when
`OW_ACME_DNS_PROVIDER` is set. Plain `docker compose up -d` in this folder works too: Compose then
takes the files from `COMPOSE_FILE` in `.env`, which must list the same files. On a development
checkout, `make start` in the repository root builds both images from source instead
(`compose.dev.yaml`); `make start PROD=1` there uses the published images.

The images come from the GitHub container registry (`ghcr.io/tsolo-io/orbitalwindow` and
`…/orbitalwindow-samba`); no login is needed.

## 3. First login

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
`make export-cert`, which saves it as `orbitalwindow.crt`. For a real certificate the name must be in DNS (a public record pointing to the LAN address is
fine) and Traefik must be able to create DNS records:

1. In `.env`: `OW_ACME_DNS_PROVIDER=<lego provider code>` (for example `cloudflare`) and
   `OW_ACME_EMAIL=<admin e-mail>`.
2. Create `acme.env` next to it (`chmod 600`) with that provider's credentials, e.g.
   `CF_DNS_API_TOKEN=…`. See the Traefik/lego documentation for each provider's variables.
3. `make start`. The certificate is requested with a DNS-01 challenge; the server does not need to
   be reachable from the internet.

## 5. Shared software

MATLAB and the Python science environment are installed once into `OW_ROOT/shared` and mounted
read-only into the desktops of users who switch them on:

```bash
make install-conda        # ~3 GB; SCIENCE_PACKAGES="python=3.13 numpy …" to change the packages
make install-matlab       # ~8 GB; no licence needed to install
make install-matlab RELEASE=R2026b PRODUCTS="MATLAB Parallel_Computing_Toolbox Deep_Learning_Toolbox"
```

Both install into `OW_ROOT/shared` from a throwaway container, so they need only Docker. Run each
once; `make install-conda FORCE=1` reinstalls conda (for MATLAB, remove `shared/matlab` first).

MATLAB is a *restricted* add-on: grant it per user (**Users → Actions → Software access**). Each user
activates their own licence in MATLAB the first time.

## 6. Backups

Schedule `make backup` (see [Operations](02-operations.md#backup-and-restore)).
