# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Admin: group/project folders. Create, members, quota, delete (folder kept unless confirmed)."""

from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import run, ui

from ..users import ValidationError
from .admin_users import show_result
from .common import PageAuth, authenticate, frame, guarded, service

COLUMNS = [
    {"name": "name", "label": "Group", "field": "name", "sortable": True, "align": "left"},
    {"name": "description", "label": "Description", "field": "description", "align": "left"},
    {"name": "members", "label": "Members", "field": "member_names", "align": "left"},
    {"name": "quota", "label": "Quota", "field": "quota", "align": "right"},
    {"name": "gid", "label": "GID", "field": "gid", "align": "right"},
    {"name": "actions", "label": "", "field": "actions", "align": "right"},
]


@ui.page("/admin/groups")
def admin_groups(request: Request) -> RedirectResponse | None:
    """Render the admin groups page.

    Args:
        request: The incoming page request.

    Returns:
        A redirect if not logged in as an admin, else None (the page is rendered).
    """
    auth = authenticate(request, admin=True)
    if isinstance(auth, RedirectResponse):
        return auth
    svc = service()
    frame(auth, "Groups")
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Groups").classes("text-h5")
            add_btn = ui.button("Add group", icon="group_add").props("no-caps")
        ui.label(
            "A group gets a shared folder: /groups/<name> in desktops and the file manager, and "
            "\\\\server\\<name> in Windows Explorer. Only members can see it; files members "
            "create stay editable by the other members."
        ).classes("text-grey-8")
        table = ui.table(columns=COLUMNS, rows=[], row_key="id").classes("w-full")
        table.add_slot(
            "body-cell-actions",
            """
            <q-td :props="props">
              <q-btn flat dense no-caps icon="group" label="Members"
                     @click="$parent.$emit('members', props.row)" />
              <q-btn flat dense no-caps icon="storage" label="Quota"
                     @click="$parent.$emit('quota', props.row)" />
              <q-btn flat dense no-caps icon="delete" label="Delete" color="negative"
                     @click="$parent.$emit('delete', props.row)" />
            </q-td>
            """,
        )

        async def refresh() -> None:
            """Reload the group table with member names and quotas."""
            groups = await run.io_bound(svc.list_groups)
            rows = []
            for group in groups:
                names = [m["name"] for m in await run.io_bound(svc.group_members, group["id"])]
                rows.append(
                    group
                    | {
                        "member_names": ", ".join(names) or "–",
                        "member_ids": [],
                        "quota": f"{group['quota_gb']:g} GB" if group["quota_gb"] else "none",
                        "created_at": None,
                    }
                )
            table.rows = rows
            table.update()

        async def on_members(event) -> None:
            """Open the members dialog of a group.

            Args:
                event: Table event with the group row.
            """
            members_dialog(auth, event.args, refresh)

        async def on_quota(event) -> None:
            """Open the quota dialog of a group.

            Args:
                event: Table event with the group row.
            """
            quota_dialog(auth, event.args, refresh)

        async def on_delete(event) -> None:
            """Open the delete dialog of a group.

            Args:
                event: Table event with the group row.
            """
            delete_dialog(auth, event.args, refresh)

        async def on_add() -> None:
            """Open the dialog to create a group."""
            create_dialog(auth, refresh)

        add_btn.on_click(guarded(auth, on_add))
        table.on("members", guarded(auth, on_members))
        table.on("quota", guarded(auth, on_quota))
        table.on("delete", guarded(auth, on_delete))
        ui.timer(0.1, guarded(auth, refresh), once=True)
    return None


def create_dialog(auth: PageAuth, refresh: Callable[[], Awaitable[None]]) -> None:
    """Open a dialog to create a group.

    Args:
        auth: Session of the page.
        refresh: Callback that reloads the group table.
    """
    svc = service()
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Add group").classes("text-h6")
        name = ui.input("Group name").props("autocapitalize=none")
        ui.label("Lowercase letters and digits. It can't be the same as a user name.").classes(
            "text-caption text-grey-7"
        )
        description = ui.input("Description (optional)")
        quota = ui.input("Quota in GB (optional)")

        async def create() -> None:
            """Create the group from the form and show the result."""
            if auth.check() is None:
                return
            try:
                result = await run.io_bound(
                    svc.create_group, auth.user.name, name.value or "", description.value or "",
                    quota.value,
                )  # fmt: skip
            except ValidationError as exc:
                ui.notify(str(exc), type="negative")
                return
            dialog.close()
            if result.warnings:
                show_result("Group created", result)
            else:
                ui.notify("Group created. Add members next.", type="positive")
            await refresh()

        with ui.row():
            ui.button("Create", on_click=create).props("no-caps")
            ui.button("Cancel", on_click=dialog.close).props("no-caps flat")
    dialog.open()


def members_dialog(auth: PageAuth, row: dict, refresh: Callable[[], Awaitable[None]]) -> None:
    """Open a dialog to choose the members of a group.

    Args:
        auth: Session of the page.
        row: Table row of the group.
        refresh: Callback that reloads the group table.
    """
    svc = service()
    users = svc.list_users()
    current = {m["id"] for m in svc.group_members(row["id"])}
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label(f"Members of {row['name']}").classes("text-h6")
        boxes = {
            user["id"]: ui.checkbox(
                f"{user['name']} ({user['display_name']})", value=user["id"] in current
            )
            for user in users
        }
        ui.label(
            "Windows file sharing and the file manager update straight away. Running desktops "
            "see the change after a restart."
        ).classes("text-caption text-grey-7")

        async def save() -> None:
            """Save the ticked users as the group's members."""
            if auth.check() is None:
                return
            chosen = [uid for uid, box in boxes.items() if box.value]
            try:
                result = await run.io_bound(
                    svc.set_group_members, auth.user.name, row["id"], chosen
                )
            except ValidationError as exc:
                ui.notify(str(exc), type="negative")
                return
            dialog.close()
            if result.warnings:
                show_result("Members saved", result)
            else:
                ui.notify("Members saved.", type="positive")
            await refresh()

        with ui.row():
            ui.button("Save", on_click=save).props("no-caps")
            ui.button("Cancel", on_click=dialog.close).props("no-caps flat")
    dialog.open()


def quota_dialog(auth: PageAuth, row: dict, refresh: Callable[[], Awaitable[None]]) -> None:
    """Open a dialog to set the soft quota of a group.

    Args:
        auth: Session of the page.
        row: Table row of the group.
        refresh: Callback that reloads the group table.
    """
    svc = service()
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label(f"Quota of {row['name']}").classes("text-h6")
        quota = ui.input("Quota in GB", value=f"{row['quota_gb']:g}" if row["quota_gb"] else "")
        ui.label("Soft quota: members get a warning, nothing is blocked. Empty means none.")\
            .classes("text-caption text-grey-7")  # fmt: skip

        async def save() -> None:
            """Save the entered quota."""
            if auth.check() is None:
                return
            try:
                await run.io_bound(svc.set_group_quota, auth.user.name, row["id"], quota.value)
            except ValidationError as exc:
                ui.notify(str(exc), type="negative")
                return
            dialog.close()
            await refresh()

        with ui.row():
            ui.button("Save", on_click=save).props("no-caps")
            ui.button("Cancel", on_click=dialog.close).props("no-caps flat")
    dialog.open()


def delete_dialog(auth: PageAuth, row: dict, refresh: Callable[[], Awaitable[None]]) -> None:
    """Open a dialog to delete a group, optionally with its folder, after typing its name.

    Args:
        auth: Session of the page.
        row: Table row of the group.
        refresh: Callback that reloads the group table.
    """
    svc = service()
    name = row["name"]
    with ui.dialog() as dialog, ui.card().classes("w-[28rem]"):
        ui.label(f"Delete the group {name}?").classes("text-h6 text-negative")
        ui.label(
            "Members lose access to the group folder and its Windows share. The folder and its "
            "files are kept unless you tick the box below."
        ).classes("text-sm")
        also_folder = ui.checkbox("Also delete the group folder and all its files")
        typed = ui.input(f"Type {name} to confirm").props("autocapitalize=none")

        async def do_delete() -> None:
            """Delete the group once its name was typed correctly, and show the result."""
            if auth.check() is None:
                return
            if (typed.value or "").strip() != name:
                ui.notify(f"Type {name} exactly to confirm.", type="warning")
                return
            result = await run.io_bound(
                svc.delete_group, auth.user.name, row["id"], also_folder.value
            )
            dialog.close()
            show_result(f"Group {name} deleted", result)
            await refresh()

        with ui.row():
            ui.button("Delete group", on_click=do_delete).props("no-caps color=negative")
            ui.button("Cancel", on_click=dialog.close).props("no-caps flat")
    dialog.open()
