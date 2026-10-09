# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Built-in user guide at /help, with the user's host, name, paths and SSH command filled in."""

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import ui

from ..help import list_pages, render
from .common import authenticate, frame, service


@ui.page("/help")
@ui.page("/help/{slug}")
def help_page(request: Request, slug: str = "") -> RedirectResponse | None:
    auth = authenticate(request)
    if isinstance(auth, RedirectResponse):
        return auth
    svc = service()
    pages = list_pages(svc.settings.help_dir)
    if not pages:
        frame(auth, "Help")
        ui.label("The user guide is not installed.").classes("p-4")
        return None
    page = next((p for p in pages if p.slug == slug), pages[0])
    host = svc.public_hostname()
    name = auth.user.name
    groups = [g["name"] for g in svc.groups_of(auth.user.id)]
    values = {
        "host": host,
        "user": name,
        "samba_path": f"\\\\{host}\\{name}",
        "ssh_cmd": svc.ssh_command(name),
        "groups": ", ".join(f"**{g}**" for g in groups) if groups else "none yet",
    }
    frame(auth, page.title)
    with ui.row().classes("w-full max-w-6xl mx-auto p-4 no-wrap items-start gap-6"):
        with ui.column().classes("w-56 shrink-0 gap-1"):
            ui.label("User guide").classes("text-subtitle1 font-medium")
            for p in pages:
                link = ui.link(p.title, f"/help/{p.slug}").classes("no-underline")
                if p is page:
                    link.classes("font-bold")
        with ui.card().classes("grow"):
            ui.markdown(render(page, values)).classes("w-full")
    return None
