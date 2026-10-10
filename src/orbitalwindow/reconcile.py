# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Background reconcile (every 30 s and at startup): make Docker match the database.

- Traefik is connected to every user network that has a desktop (lost when Traefik is recreated).
- Desktops of users that were deleted or disabled are removed (e.g. after a crash mid-action).
- Networks of deleted users are removed.
"""

import contextlib
import logging
from typing import TYPE_CHECKING

import docker

from .desktops import LABEL_MANAGED, LABEL_ROLE, LABEL_USER

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)


def reconcile(svc: Service) -> None:
    client = svc.client
    users = {u["name"]: u for u in svc.db.all("SELECT name, disabled FROM users")}

    for c in client.containers.list(
        all=True, filters={"label": [f"{LABEL_MANAGED}=true", f"{LABEL_ROLE}=desktop"]}
    ):
        name = c.labels.get(LABEL_USER, "")
        user = users.get(name)
        if user is None or user["disabled"]:
            log.warning("removing desktop %s (user %s)", c.name, "disabled" if user else "deleted")
            try:
                c.remove(force=True)
            except docker.errors.APIError as e:
                log.warning("could not remove %s: %s", c.name, e)

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
