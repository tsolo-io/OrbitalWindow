# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Admin: users. Create, reset password and stop a desktop (more actions follow in phase 3)."""

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import run, ui

from ..desktops import DesktopState
from ..service import Result
from ..users import ValidationError
from .common import PageAuth, authenticate, frame, guarded, service

COLUMNS = [
    {"name": "name", "label": "User", "field": "name", "sortable": True, "align": "left"},
    {"name": "display_name", "label": "Name", "field": "display_name", "align": "left"},
    {"name": "role", "label": "Role", "field": "role", "align": "left"},
    {"name": "desktop", "label": "Desktop", "field": "desktop", "align": "left"},
    {"name": "state", "label": "Container", "field": "state", "align": "left"},
    {"name": "limits", "label": "CPU / RAM", "field": "limits", "align": "left"},
    {"name": "actions", "label": "", "field": "actions", "align": "right"},
]


def _limits(row: dict) -> str:
    cpus = f"{row['cpus']:g} CPU" if row["cpus"] else "unlimited CPU"
    mem = f"{row['mem_gb']:g} GB" if row["mem_gb"] else "unlimited RAM"
    return f"{cpus} / {mem}"


def show_result(title: str, result: Result) -> None:
    """Show a generated password once, plus any warnings."""
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label(title).classes("text-h6")
        if result.password:
            ui.label("Give this password to the user. It is shown only once:")
            ui.code(result.password, language=None).classes("text-lg w-full")
        for w in result.warnings:
            ui.label(w).classes("text-warning")
        ui.button("Done", on_click=dialog.close).props("no-caps")
    dialog.open()


@ui.page("/admin/users")
def admin_users(request: Request) -> RedirectResponse | None:
    auth = authenticate(request, admin=True)
    if isinstance(auth, RedirectResponse):
        return auth
    frame(auth, "Users")
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Users").classes("text-h5")
            ui.button(
                "Add user", icon="person_add", on_click=lambda: create_dialog(auth, table)
            ).props("no-caps")
        table = ui.table(columns=COLUMNS, rows=[], row_key="id").classes("w-full")
        table.add_slot(
            "body-cell-actions",
            """
            <q-td :props="props">
              <q-btn flat dense no-caps icon="key" label="Reset password"
                     @click="$parent.$emit('reset', props.row)" />
              <q-btn v-if="props.row.running" flat dense no-caps icon="stop" label="Stop desktop"
                     color="negative" @click="$parent.$emit('stop', props.row)" />
            </q-td>
            """,
        )

        async def refresh() -> None:
            svc = service()
            rows = await run.io_bound(svc.list_users)
            states = await run.io_bound(svc.desktops.states)
            table.rows = [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "display_name": r["display_name"],
                    "role": ("admin" if r["is_admin"] else "user")
                    + (" (disabled)" if r["disabled"] else ""),
                    "desktop": r["image_label"] or "–",
                    "state": states.get(r["name"], DesktopState.STOPPED).value,
                    "running": r["name"] in states,
                    "limits": _limits(r),
                }
                for r in rows
            ]
            table.update()

        async def on_reset(e) -> None:
            row = e.args
            result = await run.io_bound(
                service().admin_set_password, auth.user.name, row["id"], None
            )
            show_result(f"New password for {row['name']}", result)

        async def on_stop(e) -> None:
            row = e.args
            await run.io_bound(service().stop_desktop, auth.user.name, row["id"], True)
            ui.notify(f"Stopped the desktop of {row['name']}.")
            await refresh()

        table.on("reset", guarded(auth, on_reset))
        table.on("stop", guarded(auth, on_stop))
        table.refresh_rows = refresh  # type: ignore[attr-defined]
        ui.timer(5.0, refresh)
        ui.timer(0.1, refresh, once=True)
    return None


def create_dialog(auth: PageAuth, table: ui.table) -> None:
    svc = service()
    images = {i["id"]: i["label"] for i in svc.enabled_images()}
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Add user").classes("text-h6")
        name = ui.input(
            "User name",
            validation={"2–31 lowercase letters and digits": lambda v: bool(v) and v.isalnum()},
        ).props("autocapitalize=none")
        display = ui.input("Full name")
        image = ui.select(images, label="Desktop", value=next(iter(images), None))
        is_admin = ui.checkbox("Administrator")
        ui.label("A password is generated and shown once.").classes("text-caption text-grey-7")

        async def create() -> None:
            if auth.check() is None:
                return
            try:
                result = await run.io_bound(
                    lambda: svc.create_user(
                        actor=auth.user.name,
                        name=name.value or "",
                        display_name=display.value or "",
                        is_admin=is_admin.value,
                        image_id=image.value,
                    )
                )
            except ValidationError as e:
                ui.notify(str(e), type="negative")
                return
            dialog.close()
            show_result(f"User {name.value.strip().lower()} created", result)
            await table.refresh_rows()  # type: ignore[attr-defined]

        with ui.row():
            ui.button("Create", on_click=create).props("no-caps")
            ui.button("Cancel", on_click=dialog.close).props("no-caps flat")
    dialog.open()
