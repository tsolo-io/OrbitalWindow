<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# Operations

## Users

**Users** page, **Actions** menu per user:

| Action | Effect |
|---|---|
| Reset password | New password (generated and shown once, or entered). Also changes the Windows file sharing password and signs the user out. A running desktop keeps running. |
| CPU, RAM and disk quota | Limits apply at the next desktop start. Empty = unlimited. The disk quota is soft: a warning at 90%, nothing is blocked. |
| Software access | Grant or revoke restricted add-ons (MATLAB). Revoking switches it off and stops a desktop that uses it. |
| Make / remove administrator | Admins manage everything but never get into other users' desktops or files. |
| Stop desktop | Stops and removes the container; files are kept. The user sees "stopped by an administrator". |
| Disable / enable | Disable: signed out, no login, desktop and file manager stopped, Windows sharing blocked. Enable restores access (the user starts the desktop again). |
| Delete | Removes the desktop, file manager, software image, Windows share and settings. The home folder is kept unless you tick the box. User numbers are never reused. |

You cannot disable or delete yourself or the last administrator.

## Groups

**Groups** creates a shared folder (`/groups/<name>` in desktops, `\\host\<name>` in Windows) for
its members. Windows sharing and file managers update at once; members with a running desktop see the
folder after they restart it. Deleting a group keeps its folder unless you tick the box. Group names
and user names must differ.

## Desktops and add-ons

- **Desktops**: add a LinuxServer webtop tag to build a new desktop; **Rebuild** picks up base image
  updates (do this regularly for security updates). Users' own images are rebuilt automatically
  afterwards. A desktop can only be removed when nobody uses it.
- **Other systems**: desktops and terminal-only images can be built on Ubuntu, Debian, Fedora,
  Alpine or Arch (webtop tags such as `fedora-xfce`, base images such as
  `baseimage-fedora:42`). Users' packages then come from that system. Add-ons that don't support it
  (VS Code, RStudio and MATLAB are Ubuntu/Debian only; conda not on Alpine) are greyed out for
  its users. The **Packages** column shows each image's package manager.
- **Terminal only**: choose *Terminal only* and give a LinuxServer base image with a tag (for
  example `baseimage-ubuntu:noble` or `baseimage-alpine:3.22`, or a full reference such as
  `lscr.io/linuxserver/baseimage-ubuntu:noble`)
  for users who work in the terminal and want a little more performance. It has the same GPU, CPU,
  memory and folders as a desktop, but no desktop: users reach it with **Open terminal** and SSH.
  Choosing one switches SSH on for the user (unless SSH is restricted and not granted). Add-ons
  marked as needing a desktop (VS Code, RStudio, MATLAB) are not offered with it.
- **Add-ons** are folders in `addons/` (format in `addons/README.md`). After editing one, press
  **Reload add-ons**; images that include it are rebuilt. Invalid add-ons are listed with their error.

## Monitoring

- **System**: each desktop's state and CPU/RAM use, GPUs, disk use per user/group/scratch and the
  free space on the server (warning at 85%), and the audit log of every action.
- **System → cAdvisor**: detailed container metrics (containers are listed by ID).
- **System → Logs** (Dozzle): live logs of every container. Useful first stops:
  `orbitalwindow`, `ow-traefik`, `ow-samba`, `ow-sshpiper`, `ow-<user>`.

Background jobs: every 30 s OrbitalWindow repairs drift (file managers running, Traefik and the SSH
gateway connected, leftovers of deleted users removed); disk use is measured hourly; scratch files
older than the retention are removed nightly after 02:00 (service time).

### Traefik access log

For connection problems (pages or desktops that don't load, WebSocket errors), set
`OW_TRAEFIK_ACCESS_LOG=true` in `.env` and run `make start`. Traefik then writes one JSON line
per request (time, client address, path, status, duration; no headers or cookies) to its log:

```bash
docker logs -f ow-traefik | grep '"RequestPath":"/desktop/'
```

`DownstreamStatus` 0 on `…/api/websockets` is a working stream connection; 400 or 502 there means
the desktop's stream server refused it or was not ready. Switch it off again when done: on a busy day
it grows quickly (Docker rotates it at 3 × 10 MB).

## Updating OrbitalWindow

Set `OW_VERSION` in `.env` to the new release (or keep `main` for the latest build), then, in the
deploy folder:

```bash
make update              # pulls the images and recreates only what changed
```

If the release changed the deploy files (compose files, `Makefile`, scripts), copy the new
`deploy/` from a fresh clone over the folder first; `.env` and `acme.env` are not in the repository,
so they stay as they are.
On a development checkout, `git pull && make update` rebuilds the images from source instead. Database changes are applied automatically
at start. Running desktops keep running; the web pages are unavailable for under a minute while the
service restarts. `make start` applies changes to `.env`. (`make restart` stops the
whole stack first, which interrupts every service; it is rarely needed.)

When a release changes the desktop base image (`deploy/image/`), for example to add tmux for the
browser terminal, rebuild the desktops (**Desktops → Rebuild**). Users get the new image at their
next start.

## Backup and restore

`make backup` saves homes, group folders, the state folder (database, keys) and the Samba
password database into a dated folder, and removes backups older than N days. Shared software is not
included (reinstall it with `make install-conda` / `install-matlab`). The OrbitalWindow service is stopped for a few seconds while
the database is copied; desktops keep running.

```bash
make backup DEST=/backup/orbitalwindow KEEP=14
```

Daily at 03:00, as an admin in the `docker` group (`crontab -e`):

```
0 3 * * * make -C /opt/orbitalwindow backup DEST=/backup/orbitalwindow KEEP=14 >> /var/log/ow-backup.log 2>&1
```

Copy the backup folder off the server as well. Each backup has a `SHA256SUMS` file
(`sha256sum -c SHA256SUMS`).

**Restore** (on the same or a new server with OrbitalWindow installed):

```bash
make stop
B=/backup/orbitalwindow/ow-backup-<date>
sudo tar -C /srv/orbitalwindow -xzf $B/users.tar.gz
sudo tar -C /srv/orbitalwindow -xzf $B/groups.tar.gz
sudo tar -C /srv/orbitalwindow -xzf $B/state.tar.gz
docker volume create ow-samba-lib
docker run --rm -v ow-samba-lib:/dst -v $B:/b:ro alpine:3.22 tar -C /dst -xzf /b/samba.tar.gz
make start
```

Then rebuild the desktops (**Desktops → Rebuild**); users' software images are rebuilt automatically.

## Troubleshooting

| Symptom | Check |
|---|---|
| Web page shows 404/500 for a minute | The service is starting (`make status`, `make logs`). |
| A desktop stays "Starting…" | `ow-<user>` in Logs; a desktop is *Running* once its stream server (Selkies) answers, up to a minute after the container starts. |
| Desktop tab: "websocket disconnected", console "WebSocket connection … failed" (code 1006) | The browser refuses the certificate on the stream connection. After a change of `OW_HOSTNAME` (new certificate), close all OrbitalWindow tabs and accept the certificate again; or trust it on the PC (see Installing → TLS). |
| Desktop tab: "websocket disconnected" (other cases) | Each desktop start has its own address (`/desktop/<user>/<start id>/`); an open tab moves to the new one by itself once the desktop runs. Otherwise press **Open desktop** again. If it persists, turn on the Traefik access log. |
| "Your software is being installed" | A build is running; see the user's build log or the Desktops page. |
| Windows says the password is wrong | Reset the password; Windows may cache the old one (*Credential Manager*). |
| SSH: "Connection closed" | The user's desktop is not running, or the SSH add-on is off. After 5 failed logins the gateway blocks that address for 60 minutes. |
| GPU numbers missing | AMD or no GPU (there are none), or the vendor's compose file is not active: `compose.nvidia.yaml` (NVML) or `compose.intel.yaml` (`ow-gpu-stats`; see its log). |
