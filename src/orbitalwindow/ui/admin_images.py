# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Admin: desktop images. Add a webtop tag or a terminal-only image (starts a build), enable or
disable, rebuild, remove."""

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import run, ui

from ..users import ValidationError
from .common import authenticate, frame, guarded, local_time, service
from .software import show_build_log

COLUMNS = [
    {"name": "label", "label": "Desktop", "field": "label", "align": "left"},
    {"name": "kind", "label": "Kind", "field": "kind", "align": "left"},
    {"name": "source", "label": "Built from", "field": "source", "align": "left"},
    {"name": "pkg_manager", "label": "Packages", "field": "pkg_manager", "align": "left"},
    {"name": "status", "label": "Status", "field": "status", "align": "left"},
    {"name": "built_at", "label": "Built", "field": "built_at", "align": "left"},
    {"name": "users", "label": "Users", "field": "users", "align": "right"},
    {"name": "packages", "label": "Package list", "field": "packages", "align": "right"},
    {"name": "enabled", "label": "Offered to users", "field": "enabled", "align": "center"},
    {"name": "actions", "label": "", "field": "actions", "align": "right"},
]


@ui.page("/admin/images")
def admin_images(request: Request) -> RedirectResponse | None:
    """Render the admin desktop images page.

    Args:
        request: The incoming page request.

    Returns:
        A redirect if not logged in as an admin, else None (the page is rendered).
    """
    auth = authenticate(request, admin=True)
    if isinstance(auth, RedirectResponse):
        return auth
    svc = service()
    actor = auth.user.name
    frame(auth, "Desktops")
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        ui.label("Desktops").classes("text-h5")
        ui.label(
            "Each desktop is a LinuxServer webtop image (for example ubuntu-xfce, ubuntu-kde or "
            "ubuntu-mate) with OrbitalWindow's additions. A terminal-only image has no desktop: "
            "it is built from a LinuxServer base image (for example baseimage-ubuntu:noble, "
            "baseimage-fedora:42 or baseimage-alpine:3.22) and used with the browser terminal "
            "and SSH, for a little more performance. Ubuntu, Debian, Fedora, Alpine and Arch "
            "work for both kinds. Adding one starts a build."
        ).classes("text-grey-8")
        with ui.row().classes("items-end gap-2"):
            kind = ui.toggle({True: "Desktop", False: "Terminal only"}, value=True).props("no-caps")
            tag = ui.input("Webtop tag", placeholder="ubuntu-mate").props("dense outlined")
            tag.classes("w-72")
            label = ui.input("Name shown to users (optional)").props("dense outlined")
            add_btn = ui.button("Add", icon="add").props("no-caps")

        def on_kind() -> None:
            """Ask for a webtop tag or a LinuxServer image, depending on the kind."""
            if kind.value:
                tag.props('label="Webtop tag" placeholder="ubuntu-mate"')
            else:
                tag.props('label="LinuxServer image" placeholder="baseimage-fedora:42"')

        kind.on_value_change(lambda _: on_kind())

        table = ui.table(columns=COLUMNS, rows=[], row_key="id").classes("w-full")
        table.add_slot(
            "body-cell-status",
            """
            <q-td :props="props">
              <q-badge :color="{ready: 'positive', building: 'orange', failed: 'negative'}
                               [props.row.status] || 'grey'" :label="props.row.status" />
            </q-td>
            """,
        )
        table.add_slot(
            "body-cell-enabled",
            """
            <q-td :props="props">
              <q-toggle :model-value="props.row.enabled" dense
                        @update:model-value="v => $parent.$emit('enable', {id: props.row.id, v})" />
            </q-td>
            """,
        )
        table.add_slot(
            "body-cell-actions",
            """
            <q-td :props="props">
              <q-btn flat dense no-caps icon="article" label="Log"
                     @click="$parent.$emit('log', props.row)" />
              <q-btn flat dense no-caps icon="refresh" label="Rebuild"
                     :disable="props.row.status === 'building'"
                     @click="$parent.$emit('rebuild', props.row)" />
              <q-btn flat dense no-caps icon="delete" label="Remove" color="negative"
                     :disable="props.row.users > 0"
                     @click="$parent.$emit('remove', props.row)" />
            </q-td>
            """,
        )

        async def refresh() -> None:
            """Reload the image table."""
            rows = await run.io_bound(svc.list_images)
            table.rows = [
                row
                | {
                    "built_at": local_time(row["built_at"], empty="–"),
                    "kind": "Desktop" if row["gui"] else "Terminal only",
                    "source": row["base_image"] or f"webtop:{row['base_tag']}",
                }
                for row in rows
            ]
            table.update()

        async def act(fn, *args, done: str | None = None) -> None:
            """Run a service call as the admin, show errors or ``done``, then refresh the table.

            Args:
                fn: Service function called as ``fn(actor, *args)``.
                *args: Further arguments for ``fn``.
                done: Message to show on success; nothing if None.
            """
            try:
                await run.io_bound(fn, actor, *args)
            except ValidationError as exc:
                ui.notify(str(exc), type="negative")
            else:
                if done:
                    ui.notify(done)
            await refresh()

        async def on_add() -> None:
            """Add the image from the form (starts a build) and clear the form."""
            add = svc.add_image if kind.value else svc.add_terminal_image
            await act(add, tag.value or "", label.value or None, done="Build started.")
            tag.set_value("")
            label.set_value("")

        async def on_enable(event) -> None:
            """Offer or withdraw an image from the table toggle.

            Args:
                event: Table event with ``id`` and the new value ``v``.
            """
            await act(svc.set_image_enabled, event.args["id"], bool(event.args["v"]))

        async def on_rebuild(event) -> None:
            """Start a rebuild of an image.

            Args:
                event: Table event with the image row.
            """
            await act(svc.rebuild_image, event.args["id"], done="Rebuild started.")

        async def on_remove(event) -> None:
            """Ask for confirmation, then remove an image.

            Args:
                event: Table event with the image row.
            """
            row = event.args
            with ui.dialog() as dialog, ui.card():
                ui.label(f"Remove the desktop {row['label']} ({row['base_tag']})?")
                with ui.row():
                    ui.button("Remove", color="negative", on_click=lambda: dialog.submit(True)
                              ).props("no-caps")  # fmt: skip
                    ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("no-caps flat")
            if await dialog:
                await act(svc.remove_image, row["id"], done="Removed.")

        def on_log(event) -> None:
            """Show the latest build log of an image.

            Args:
                event: Table event with the image row.
            """
            build = svc.builder.latest_build("base", event.args["id"])
            if build is None:
                ui.notify("No build yet.")
                return
            show_build_log(build["id"], f"Build log: {event.args['label']}")

        add_btn.on_click(guarded(auth, on_add))
        table.on("enable", guarded(auth, on_enable))
        table.on("rebuild", guarded(auth, on_rebuild))
        table.on("remove", guarded(auth, on_remove))
        table.on("log", guarded(auth, on_log))
        ui.timer(5.0, guarded(auth, refresh))
        ui.timer(0.1, guarded(auth, refresh), once=True)
    return None
