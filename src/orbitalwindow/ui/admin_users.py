# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Admin: users. Create, reset password, disable/enable, delete, stop desktop, limits, admin
rights and software access (grants of restricted add-ons)."""

from collections.abc import Awaitable, Callable
from typing import Any

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
    {"name": "account", "label": "Account", "field": "account", "align": "left"},
    {"name": "desktop", "label": "Desktop", "field": "desktop", "align": "left"},
    {"name": "state", "label": "Container", "field": "state", "align": "left"},
    {"name": "limits", "label": "CPU / RAM limit", "field": "limits", "align": "left"},
    {"name": "actions", "label": "", "field": "actions", "align": "right"},
]

ACTIONS_SLOT = """
<q-td :props="props">
  <q-btn flat dense no-caps icon="more_vert" label="Actions">
    <q-menu auto-close>
      <q-list dense style="min-width: 220px">
        <q-item clickable @click="$parent.$emit('act', {a: 'password', row: props.row})">
          <q-item-section avatar><q-icon name="key" /></q-item-section>
          <q-item-section>Reset password</q-item-section></q-item>
        <q-item clickable @click="$parent.$emit('act', {a: 'limits', row: props.row})">
          <q-item-section avatar><q-icon name="speed" /></q-item-section>
          <q-item-section>CPU, RAM and disk quota</q-item-section></q-item>
        <q-item clickable @click="$parent.$emit('act', {a: 'grants', row: props.row})">
          <q-item-section avatar><q-icon name="apps" /></q-item-section>
          <q-item-section>Software access</q-item-section></q-item>
        <q-item clickable @click="$parent.$emit('act', {a: 'rename', row: props.row})">
          <q-item-section avatar><q-icon name="badge" /></q-item-section>
          <q-item-section>Change name</q-item-section></q-item>
        <q-item clickable @click="$parent.$emit('act', {a: 'admin', row: props.row})">
          <q-item-section avatar><q-icon name="admin_panel_settings" /></q-item-section>
          <q-item-section>{{ props.row.is_admin ? 'Remove administrator rights'
                                                : 'Make administrator' }}</q-item-section></q-item>
        <q-separator />
        <q-item v-if="props.row.running" clickable
                @click="$parent.$emit('act', {a: 'stop', row: props.row})">
          <q-item-section avatar><q-icon name="stop" color="negative" /></q-item-section>
          <q-item-section>Stop desktop</q-item-section></q-item>
        <q-item clickable @click="$parent.$emit('act', {a: 'disable', row: props.row})">
          <q-item-section avatar><q-icon :name="props.row.disabled ? 'lock_open' : 'block'" />
          </q-item-section>
          <q-item-section>{{ props.row.disabled ? 'Enable account' : 'Disable account' }}
          </q-item-section></q-item>
        <q-item clickable @click="$parent.$emit('act', {a: 'delete', row: props.row})">
          <q-item-section avatar><q-icon name="delete" color="negative" /></q-item-section>
          <q-item-section class="text-negative">Delete user</q-item-section></q-item>
      </q-list>
    </q-menu>
  </q-btn>
</q-td>
"""


def _limits(row: dict) -> str:
    cpus = f"{row['cpus']:g} CPU" if row["cpus"] else "unlimited"
    mem = f"{row['mem_gb']:g} GB" if row["mem_gb"] else "unlimited"
    return f"{cpus} / {mem}"


def show_result(title: str, result: Result) -> None:
    """Show a generated password once, plus any notes or warnings."""
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label(title).classes("text-h6")
        if result.password:
            ui.label("Give this password to the user. It is shown only once:")
            ui.code(result.password, language=None).classes("text-lg w-full")
        for w in result.warnings:
            ui.label(w).classes("text-sm text-grey-9")
        ui.button("Done", on_click=dialog.close).props("no-caps")
    dialog.open()


def confirm(title: str, text: str, button: str, *, danger: bool = False) -> ui.dialog:
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label(title).classes("text-h6")
        if text:
            ui.label(text)
        with ui.row():
            ui.button(button, on_click=lambda: dialog.submit(True)).props(
                "no-caps" + (" color=negative" if danger else "")
            )
            ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("no-caps flat")
    return dialog


@ui.page("/admin/users")
def admin_users(request: Request) -> RedirectResponse | None:
    auth = authenticate(request, admin=True)
    if isinstance(auth, RedirectResponse):
        return auth
    svc = service()
    frame(auth, "Users")
    with ui.column().classes("w-full max-w-7xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Users").classes("text-h5")
            add_btn = ui.button("Add user", icon="person_add").props("no-caps")
        table = ui.table(columns=COLUMNS, rows=[], row_key="id").classes("w-full")
        table.add_slot("body-cell-actions", ACTIONS_SLOT)
        table.add_slot(
            "body-cell-account",
            """<q-td :props="props"><q-badge :color="props.row.disabled ? 'negative' : 'positive'"
               :label="props.row.account" /></q-td>""",
        )

        async def refresh() -> None:
            rows = await run.io_bound(svc.list_users)
            states = await run.io_bound(svc.desktops.states)
            table.rows = [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "display_name": r["display_name"],
                    "is_admin": r["is_admin"],
                    "disabled": r["disabled"],
                    "role": "admin" if r["is_admin"] else "user",
                    "account": "disabled" if r["disabled"] else "active",
                    "desktop": r["image_label"] or "–",
                    "state": states.get(r["name"], DesktopState.STOPPED).value,
                    "running": r["name"] in states,
                    "limits": _limits(r),
                    "cpus": r["cpus"],
                    "mem_gb": r["mem_gb"],
                    "quota_gb": r["quota_gb"],
                }
                for r in rows
            ]
            table.update()

        async def call(fn: Callable[..., Any], *args: Any) -> Any:
            """Run a service call; show validation errors; refresh the table afterwards."""
            try:
                return await run.io_bound(fn, *args)
            except ValidationError as e:
                ui.notify(str(e), type="negative")
                return None
            finally:
                await refresh()

        actor, me = auth.user.name, auth.user.id

        async def on_action(e) -> None:
            action, row = e.args["a"], e.args["row"]
            uid, name = row["id"], row["name"]
            if action == "password":
                password_dialog(auth, uid, name)
            elif action == "limits":
                limits_dialog(auth, row, refresh)
            elif action == "grants":
                grants_dialog(auth, uid, name)
            elif action == "rename":
                rename_dialog(auth, row, refresh)
            elif action == "admin":
                title = (
                    f"Remove administrator rights from {name}?"
                    if row["is_admin"]
                    else f"Make {name} an administrator?"
                )
                if await confirm(title, "", "Yes"):
                    await call(svc.set_admin, actor, me, uid, not row["is_admin"])
            elif action == "stop":
                if await confirm(
                    f"Stop the desktop of {name}?",
                    "Unsaved work in their desktop is lost. Their files are kept.",
                    "Stop desktop",
                    danger=True,
                ):
                    await call(svc.stop_desktop, actor, uid, True)
                    ui.notify(f"Stopped the desktop of {name}.")
            elif action == "disable":
                if row["disabled"]:
                    result = await call(svc.set_disabled, actor, me, uid, False)
                    if result:
                        ui.notify(f"{name} can log in again.")
                elif await confirm(
                    f"Disable {name}?",
                    "They are signed out, their desktop is stopped and Windows file sharing is "
                    "blocked. Their files are kept.",
                    "Disable",
                    danger=True,
                ):
                    result = await call(svc.set_disabled, actor, me, uid, True)
                    if result and result.warnings:
                        show_result(f"{name} disabled", result)
                    elif result:
                        ui.notify(f"{name} is disabled.")
            elif action == "delete":
                delete_dialog(auth, uid, name, refresh)

        add_btn.on_click(guarded(auth, lambda: _open_create(auth, refresh)))
        table.on("act", guarded(auth, on_action))
        ui.timer(5.0, guarded(auth, refresh))
        ui.timer(0.1, guarded(auth, refresh), once=True)
    return None


async def _open_create(auth: PageAuth, refresh: Callable[[], Awaitable[None]]) -> None:
    create_dialog(auth, refresh)


def create_dialog(auth: PageAuth, refresh: Callable[[], Awaitable[None]]) -> None:
    svc = service()
    images = {i["id"]: i["label"] for i in svc.enabled_images()}
    settings = svc.get_settings()
    restricted = [a for a in svc.software.enabled_addons() if svc.software.is_restricted(a)]
    with ui.dialog() as dialog, ui.card().classes("w-[28rem]"):
        ui.label("Add user").classes("text-h6")
        name = ui.input("User name").props("autocapitalize=none")
        ui.label("Lowercase letters and digits, starting with a letter.").classes(
            "text-caption text-grey-7"
        )
        display = ui.input("Full name")
        image = ui.select(images, label="Desktop", value=next(iter(images), None))
        with ui.row().classes("w-full no-wrap"):
            cpus = ui.input("CPUs", value=settings.get("default_cpus", "")).classes("w-1/2")
            mem = ui.input("RAM (GB)", value=settings.get("default_mem_gb", "")).classes("w-1/2")
        ui.label("Leave CPU or RAM empty for unlimited.").classes("text-caption text-grey-7")
        is_admin = ui.checkbox("Administrator")
        grants = [ui.checkbox(f"Allow {a.spec.label}") for a in restricted]
        ui.label("A password is generated and shown once.").classes("text-caption text-grey-7")

        async def create() -> None:
            if auth.check() is None:
                return

            def work() -> Result:
                result = svc.create_user(
                    actor=auth.user.name,
                    name=name.value or "",
                    display_name=display.value or "",
                    is_admin=is_admin.value,
                    image_id=image.value,
                    cpus=cpus.value or None,
                    mem_gb=mem.value or None,
                )
                user = svc.db.one(
                    "SELECT id FROM users WHERE name = ?", [(name.value or "").strip().lower()]
                )
                for addon, box in zip(restricted, grants, strict=True):
                    if box.value and user:
                        svc.grant_addon(auth.user.name, user["id"], addon.key)
                return result

            try:
                result = await run.io_bound(work)
            except ValidationError as e:
                ui.notify(str(e), type="negative")
                return
            dialog.close()
            show_result(f"User {name.value.strip().lower()} created", result)
            await refresh()

        with ui.row():
            ui.button("Create", on_click=create).props("no-caps")
            ui.button("Cancel", on_click=dialog.close).props("no-caps flat")
    dialog.open()


def password_dialog(auth: PageAuth, user_id: int, name: str) -> None:
    svc = service()
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label(f"Reset the password of {name}").classes("text-h6")
        ui.label(
            "This also changes their Windows file sharing password and signs them out. "
            "A running desktop is not affected."
        ).classes("text-sm text-grey-8")
        mode = ui.radio({"gen": "Generate a password", "set": "Enter a password"}, value="gen")
        pw = ui.input("New password", password=True, password_toggle_button=True).props(
            "autocomplete=new-password"
        )
        pw.bind_visibility_from(mode, "value", value="set")

        async def do_reset() -> None:
            if auth.check() is None:
                return
            new = pw.value if mode.value == "set" else None
            try:
                result = await run.io_bound(svc.admin_set_password, auth.user.name, user_id, new)
            except ValidationError as e:
                ui.notify(str(e), type="negative")
                return
            dialog.close()
            if result.password or result.warnings:
                show_result(f"New password for {name}", result)
            else:
                ui.notify("Password changed.")

        with ui.row():
            ui.button("Reset password", on_click=do_reset).props("no-caps")
            ui.button("Cancel", on_click=dialog.close).props("no-caps flat")
    dialog.open()


def limits_dialog(auth: PageAuth, row: dict, refresh: Callable[[], Awaitable[None]]) -> None:
    svc = service()
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label(f"Limits for {row['name']}").classes("text-h6")
        cpus = ui.input("CPUs", value=f"{row['cpus']:g}" if row["cpus"] else "")
        mem = ui.input("RAM (GB)", value=f"{row['mem_gb']:g}" if row["mem_gb"] else "")
        ui.label("Empty means unlimited. Changes apply the next time the desktop starts.").classes(
            "text-caption text-grey-7"
        )
        quota = ui.input(
            "Disk quota (GB)", value=f"{row['quota_gb']:g}" if row.get("quota_gb") else ""
        )
        ui.label("Soft quota: the user is warned at 90%, nothing is blocked. Empty means none.")\
            .classes("text-caption text-grey-7")  # fmt: skip

        async def save() -> None:
            if auth.check() is None:
                return
            try:
                await run.io_bound(svc.set_limits, auth.user.name, row["id"], cpus.value, mem.value)
                await run.io_bound(svc.set_quota, auth.user.name, row["id"], quota.value)
            except ValidationError as e:
                ui.notify(str(e), type="negative")
                return
            dialog.close()
            ui.notify("Saved. The new limits apply at the next start.")
            await refresh()

        with ui.row():
            ui.button("Save", on_click=save).props("no-caps")
            ui.button("Cancel", on_click=dialog.close).props("no-caps flat")
    dialog.open()


def rename_dialog(auth: PageAuth, row: dict, refresh: Callable[[], Awaitable[None]]) -> None:
    svc = service()
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label(f"Name of {row['name']}").classes("text-h6")
        display = ui.input("Full name", value=row["display_name"])

        async def save() -> None:
            if auth.check() is None:
                return
            try:
                await run.io_bound(svc.set_display_name, auth.user.name, row["id"], display.value)
            except ValidationError as e:
                ui.notify(str(e), type="negative")
                return
            dialog.close()
            await refresh()

        with ui.row():
            ui.button("Save", on_click=save).props("no-caps")
            ui.button("Cancel", on_click=dialog.close).props("no-caps flat")
    dialog.open()


def delete_dialog(
    auth: PageAuth, user_id: int, name: str, refresh: Callable[[], Awaitable[None]]
) -> None:
    svc = service()
    with ui.dialog() as dialog, ui.card().classes("w-[28rem]"):
        ui.label(f"Delete {name}?").classes("text-h6 text-negative")
        ui.label(
            "This removes their desktop, software, Windows share and settings. It cannot be "
            "undone. Their home folder is kept unless you tick the box below."
        ).classes("text-sm")
        also_home = ui.checkbox("Also delete their home folder and all their files")
        typed = ui.input(f"Type {name} to confirm").props("autocapitalize=none")

        async def do_delete() -> None:
            if auth.check() is None:
                return
            if (typed.value or "").strip() != name:
                ui.notify(f"Type {name} exactly to confirm.", type="warning")
                return
            try:
                result = await run.io_bound(
                    svc.delete_user, auth.user.name, auth.user.id, user_id, also_home.value
                )
            except ValidationError as e:
                ui.notify(str(e), type="negative")
                return
            dialog.close()
            show_result(f"{name} deleted", result)
            await refresh()

        with ui.row():
            ui.button("Delete user", on_click=do_delete).props("no-caps color=negative")
            ui.button("Cancel", on_click=dialog.close).props("no-caps flat")
    dialog.open()


def grants_dialog(auth: PageAuth, user_id: int, name: str) -> None:
    """Grant or revoke restricted add-ons for one user."""
    svc = service()
    restricted = [a for a in svc.software.enabled_addons() if svc.software.is_restricted(a)]
    granted = svc.grants(user_id)
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label(f"Software access for {name}").classes("text-h6")
        if not restricted:
            ui.label("There are no restricted add-ons.")
        ui.label(
            "Restricted add-ons need your permission. Open add-ons are available to everyone."
        ).classes("text-caption text-grey-7")
        for addon in restricted:
            box = ui.checkbox(addon.spec.label, value=addon.key in granted)

            async def toggle(e, key: str = addon.key) -> None:
                if auth.check() is None:
                    return
                if e.value:
                    await run.io_bound(svc.grant_addon, auth.user.name, user_id, key)
                    ui.notify("Granted. The user can now switch it on.")
                else:
                    result = await run.io_bound(svc.revoke_addon, auth.user.name, user_id, key)
                    ui.notify(" ".join(["Revoked.", *result.warnings]))

            box.on_value_change(toggle)
        ui.button("Done", on_click=dialog.close).props("no-caps")
    dialog.open()
