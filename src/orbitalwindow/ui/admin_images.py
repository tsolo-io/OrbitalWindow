# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Admin: desktop images. Add a webtop tag (starts a build), enable/disable, rebuild, remove."""

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import run, ui

from ..users import ValidationError
from .common import authenticate, frame, guarded, service
from .software import show_build_log

COLUMNS = [
    {"name": "label", "label": "Desktop", "field": "label", "align": "left"},
    {"name": "base_tag", "label": "Webtop tag", "field": "base_tag", "align": "left"},
    {"name": "status", "label": "Status", "field": "status", "align": "left"},
    {"name": "built_at", "label": "Built", "field": "built_at", "align": "left"},
    {"name": "users", "label": "Users", "field": "users", "align": "right"},
    {"name": "packages", "label": "Packages", "field": "packages", "align": "right"},
    {"name": "enabled", "label": "Offered to users", "field": "enabled", "align": "center"},
    {"name": "actions", "label": "", "field": "actions", "align": "right"},
]


@ui.page("/admin/images")
def admin_images(request: Request) -> RedirectResponse | None:
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
            "ubuntu-mate) with OrbitalWindow's additions. Adding one starts a build."
        ).classes("text-grey-8")
        with ui.row().classes("items-end gap-2"):
            tag = ui.input("Webtop tag", placeholder="ubuntu-mate").props("dense outlined")
            label = ui.input("Name shown to users (optional)").props("dense outlined")
            add_btn = ui.button("Add desktop", icon="add").props("no-caps")

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
            rows = await run.io_bound(svc.list_images)
            table.rows = [
                r | {"built_at": r["built_at"].strftime("%Y-%m-%d %H:%M") if r["built_at"] else "–"}
                for r in rows
            ]
            table.update()

        async def act(fn, *args, done: str | None = None) -> None:
            try:
                await run.io_bound(fn, actor, *args)
            except ValidationError as e:
                ui.notify(str(e), type="negative")
            else:
                if done:
                    ui.notify(done)
            await refresh()

        async def on_add() -> None:
            await act(svc.add_image, tag.value or "", label.value or None, done="Build started.")
            tag.set_value("")
            label.set_value("")

        async def on_enable(e) -> None:
            await act(svc.set_image_enabled, e.args["id"], bool(e.args["v"]))

        async def on_rebuild(e) -> None:
            await act(svc.rebuild_image, e.args["id"], done="Rebuild started.")

        async def on_remove(e) -> None:
            row = e.args
            with ui.dialog() as dialog, ui.card():
                ui.label(f"Remove the desktop {row['label']} ({row['base_tag']})?")
                with ui.row():
                    ui.button("Remove", color="negative", on_click=lambda: dialog.submit(True)
                              ).props("no-caps")  # fmt: skip
                    ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("no-caps flat")
            if await dialog:
                await act(svc.remove_image, row["id"], done="Removed.")

        def on_log(e) -> None:
            build = svc.builder.latest_build("base", e.args["id"])
            if build is None:
                ui.notify("No build yet.")
                return
            show_build_log(build["id"], f"Build log: {e.args['label']}")

        add_btn.on_click(guarded(auth, on_add))
        table.on("enable", guarded(auth, on_enable))
        table.on("rebuild", guarded(auth, on_rebuild))
        table.on("remove", guarded(auth, on_remove))
        table.on("log", guarded(auth, on_log))
        ui.timer(5.0, guarded(auth, refresh))
        ui.timer(0.1, guarded(auth, refresh), once=True)
    return None
