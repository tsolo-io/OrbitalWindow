# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Admin: system backups (#82). Daily time, how many to keep, back up now, download."""

from datetime import datetime

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import run, ui

from ..metrics import fmt_bytes
from ..users import ValidationError
from .common import authenticate, frame, guarded, local_time, service


@ui.page("/admin/backups")
def admin_backups(request: Request) -> RedirectResponse | None:
    """Render the backups page.

    Args:
        request: The incoming page request.

    Returns:
        A redirect if not logged in as an admin, else None (the page is rendered).
    """
    auth = authenticate(request, admin=True)
    if isinstance(auth, RedirectResponse):
        return auth
    backups = service().backups
    config = backups.config()
    frame(auth, "Backups")
    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-4"):
        ui.label("Backups").classes("text-h5")
        ui.label(
            "A backup holds the databases (users, settings, chat), the server's certificate "
            "authority, the Samba passwords and the SSH keys. It does not hold the users' home "
            "folders or the group folders. Backups are kept on this server in "
            f"{backups.folder}: copy them to another place yourself, for example with the "
            "Download links below."
        ).classes("text-body2")

        with ui.card().classes("w-full"):
            ui.label("Daily backup").classes("text-h6")
            enabled = ui.switch("Make a backup every day", value=config["enabled"])
            off_warning = ui.label(
                "With daily backups off, nothing can be restored if the server's disk fails."
            ).classes("text-orange-9")
            off_warning.bind_visibility_from(enabled, "value", backward=lambda on: not on)
            with ui.row().classes("items-start gap-4"):
                time = ui.input("Time (HH:MM, server time)", value=config["time"])
                keep = ui.number("Backups to keep", value=config["keep"], min=1, max=100, step=1)
            ui.label(
                f"Server time zone: {service().settings.tz}. After each backup the oldest ones "
                "are deleted, so that this number is kept."
            ).classes("text-caption text-grey-7")

            async def save() -> None:
                """Save the settings; switching the daily backup off needs a confirmation."""
                if config["enabled"] and not enabled.value:
                    with ui.dialog() as dialog, ui.card().classes("w-96"):
                        ui.label("Switch off daily backups?").classes("text-h6")
                        ui.label(
                            "If the server's disk fails, the users, settings, chats and the "
                            "certificate can't be restored. Users would have to install a new "
                            "certificate."
                        )
                        with ui.row().classes("justify-end w-full"):
                            ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat")
                            ui.button("Switch off", color="negative",
                                      on_click=lambda: dialog.submit(True))  # fmt: skip
                    if not await dialog:
                        return
                try:
                    await run.io_bound(
                        backups.configure,
                        auth.user.name,
                        bool(enabled.value),
                        time.value or "",
                        int(keep.value or 0),
                    )
                except ValidationError as exc:
                    ui.notify(str(exc), type="negative")
                    return
                config.update(backups.config())
                ui.notify("Backup settings saved.", type="positive")
                refresh()

            ui.button("Save", icon="save", on_click=guarded(auth, save)).props("no-caps")

        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("Backups on this server").classes("text-h6")
                now_button = ui.button("Back up now", icon="backup").props("no-caps")
            status = ui.label().classes("text-body2")
            table = ui.column().classes("w-full gap-1")

        def refresh() -> None:
            """Show the last result and the list of backups."""
            current = backups.config()
            if backups.running:
                status.text = "A backup is running…"
                status.classes(replace="text-body2")
            elif current["last_status"] == "failed":
                when = local_time(datetime.fromisoformat(current["last_at"]))
                status.text = f"The last backup ({when}) failed: {current['last_error']}"
                status.classes(replace="text-body2 text-negative")
            elif current["last_at"]:
                when = local_time(datetime.fromisoformat(current["last_at"]))
                status.text = f"Last backup: {when}, {current['last_file']}."
                status.classes(replace="text-body2")
            else:
                status.text = "No backup has been made yet."
                status.classes(replace="text-body2")
            now_button.set_enabled(not backups.running)
            table.clear()
            with table:
                rows = backups.entries()
                if not rows:
                    ui.label("None yet.").classes("text-grey-7")
                for row in rows:
                    with ui.row().classes("items-center justify-between w-full"):
                        ui.label(local_time(row["time"])).classes("w-40")
                        ui.label(fmt_bytes(row["size"])).classes("w-24 text-right")
                        ui.link("Download", f"/admin/backups/{row['name']}")

        async def back_up_now() -> None:
            """Make a backup now and report the outcome."""
            now_button.set_enabled(False)
            status.text = "A backup is running…"
            try:
                name = await run.io_bound(backups.run, auth.user.name)
            except RuntimeError as exc:
                ui.notify(f"The backup failed: {exc}", type="negative")
            else:
                ui.notify(f"Backup made: {name}", type="positive")
            refresh()

        now_button.on_click(guarded(auth, back_up_now))
        refresh()
        ui.timer(10, guarded(auth, refresh))
    return None
