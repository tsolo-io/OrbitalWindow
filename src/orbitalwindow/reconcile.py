# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Background reconcile (every 30 s and at startup): make Docker match the database.

- Every enabled user has a running file manager; disabled users' file managers are stopped.
- Desktops and file managers of users that were deleted, and desktops of disabled users, are
  removed (e.g. after a crash mid-action). Networks of deleted users are removed.
- Traefik is connected to every user network that has a member (lost when Traefik is recreated).
- Scheduled jobs: disk usage (hourly) and the scratch clean-up (nightly).
"""

import contextlib
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import docker

from .desktops import LABEL_MANAGED, LABEL_ROLE, LABEL_USER

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

DISK_EVERY = timedelta(hours=1)
SCRATCH_CLEAN_HOUR = 2  # local time; runs once a day at or after this hour


def reconcile(svc: Service, now: datetime | None = None) -> None:
    client = svc.client
    users = {u["name"]: u for u in svc.db.all("SELECT id, name, disabled FROM users")}

    for c in client.containers.list(all=True, filters={"label": f"{LABEL_MANAGED}=true"}):
        role = c.labels.get(LABEL_ROLE)
        if role not in ("desktop", "files"):
            continue
        name = c.labels.get(LABEL_USER, "")
        user = users.get(name)
        if user is None or (user["disabled"] and role == "desktop"):
            log.warning("removing %s (user %s)", c.name, "disabled" if user else "deleted")
            try:
                c.remove(force=True)
            except docker.errors.APIError as e:
                log.warning("could not remove %s: %s", c.name, e)

    for user in users.values():
        if user["disabled"]:
            svc.files.stop(user["name"])
        else:
            for warning in svc.ensure_files(user["id"]):
                log.warning("%s: %s", user["name"], warning)

    for net in client.networks.list(filters={"label": f"{LABEL_MANAGED}=true"}):
        name = (net.attrs.get("Labels") or {}).get(LABEL_USER)
        if name and name not in users:
            net.reload()
            for c in net.containers:
                with contextlib.suppress(docker.errors.APIError):
                    net.disconnect(c, force=True)
            try:
                net.remove()
                log.info("removed network %s of deleted user %s", net.name, name)
            except docker.errors.APIError as e:
                log.warning("could not remove network %s: %s", net.name, e)

    fixed = svc.desktops.ensure_traefik_connected()
    if fixed:
        log.info("reconnected Traefik to %d user network(s)", fixed)

    run_scheduled(svc, now or datetime.now())


def _due(svc: Service, key: str) -> datetime | None:
    value = svc.db.get_setting(key)
    return datetime.fromisoformat(value) if value else None


def run_scheduled(svc: Service, now: datetime) -> None:
    """Hourly disk measurement; scratch clean-up once a day after SCRATCH_CLEAN_HOUR."""
    last = _due(svc, "last_disk_measure")
    if last is None or now - last >= DISK_EVERY:
        svc.db.set_setting("last_disk_measure", now.isoformat(timespec="seconds"))
        try:
            svc.measure_disk()
        except docker.errors.DockerException as e:
            log.warning("disk usage measurement failed: %s", e)
    last = _due(svc, "last_scratch_clean")
    if now.hour >= SCRATCH_CLEAN_HOUR and (last is None or last.date() < now.date()):
        svc.db.set_setting("last_scratch_clean", now.isoformat(timespec="seconds"))
        try:
            svc.clean_scratch()
        except docker.errors.DockerException as e:
            log.warning("scratch clean-up failed: %s", e)
