# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The environment variables page: the user's own variables for every desktop (ADR 0032)."""

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import run, ui

from ..users import ValidationError
from .common import PageAuth, authenticate, frame, guarded, help_icon, service


@ui.page("/environment")
def environment_page(request: Request) -> RedirectResponse | None:
    """Render the list of the user's environment variables, with add, change and remove.

    Args:
        request: The incoming page request.

    Returns:
        A redirect to the login page if not logged in, else None (the page is rendered).
    """
    auth = authenticate(request)
    if isinstance(auth, RedirectResponse):
        return auth
    frame(auth, "Environment variables")
    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-4"):
        ui.link("← My desktop", "/").classes("text-sm")
        environment_card(auth)
    return None


def environment_card(auth: PageAuth) -> None:
    """Add the card with the variables, the restart note and the form to add one.

    Args:
        auth: Session of the page.
    """
    svc = service()
    user_id = auth.user.id
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-1"):
            ui.label("My environment variables").classes("text-h6")
            help_icon("environment")
        ui.label(
            "These are set in every desktop you start, also after you switch desktops, and in "
            "the terminal and SSH. Changes apply the next time you start your desktop."
        ).classes("text-sm text-grey-8")
        restart_note = ui.label(
            "Your variables have changed. Stop and start your desktop to use them."
        ).classes("text-caption text-orange-9")
        rows = ui.column().classes("w-full gap-1")
        ui.separator()
        ui.label("Add a variable").classes("font-medium")
        with ui.row().classes("w-full items-start gap-2"):
            name_in = ui.input("Name", placeholder="HF_HOME").props("dense outlined")
            value_in = (
                ui.input("Value", placeholder="/config/huggingface")
                .props("dense outlined")
                .classes("grow")
            )
            add_btn = ui.button("Add", icon="add").props("no-caps")

    async def render() -> None:
        """Rebuild the list of variables and update the restart note."""
        variables = await run.io_bound(svc.user_env, user_id)
        restart = await run.io_bound(svc.env_restart_needed, user_id)
        if variables is None or restart is None:  # the page closed or the app is stopping
            return
        restart_note.set_visibility(restart)
        rows.clear()
        with rows:
            if not variables:
                ui.label("You have no variables yet.").classes("text-sm text-grey-7")
            for variable in variables:
                with ui.row().classes("w-full items-center no-wrap gap-2"):
                    ui.label(variable["name"]).classes("font-mono font-medium w-48 break-all")
                    ui.label(variable["value"]).classes("font-mono text-sm grow break-all")
                    on_edit = _edit(variable["name"], variable["value"])
                    ui.button(icon="edit", on_click=guarded(auth, on_edit)).props(
                        "flat round dense"
                    ).tooltip("Change")
                    ui.button(icon="delete", on_click=guarded(auth, _remove(variable["name"])))\
                        .props("flat round dense color=negative").tooltip("Remove")  # fmt: skip

    def _edit(name: str, value: str):
        """Make a click handler that opens the change dialog for one variable.

        Args:
            name: The variable's current name.
            value: Its current value.

        Returns:
            A click handler.
        """

        def handler() -> None:
            """Open a dialog to change the variable's name or value."""
            with ui.dialog() as dialog, ui.card().classes("w-[32rem]"):
                ui.label(f"Change {name}").classes("text-h6")
                new_name = ui.input("Name", value=name).props("dense outlined").classes("w-full")
                new_value = ui.input("Value", value=value).props("dense outlined").classes("w-full")

                async def save() -> None:
                    """Save the change and close the dialog."""
                    try:
                        await run.io_bound(
                            svc.set_user_env, auth.user.name, user_id,
                            new_name.value or "", new_value.value or "", name,
                        )  # fmt: skip
                    except ValidationError as exc:
                        ui.notify(str(exc), type="negative")
                        return
                    dialog.close()
                    ui.notify("Saved.", type="positive")
                    await render()

                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
                    ui.button("Save", on_click=guarded(auth, save)).props("no-caps")
            dialog.open()

        return handler

    def _remove(name: str):
        """Make a click handler that removes one variable.

        Args:
            name: The variable's name.

        Returns:
            An async click handler.
        """

        async def handler() -> None:
            """Remove the variable and re-render the list."""
            await run.io_bound(svc.remove_user_env, auth.user.name, user_id, name)
            ui.notify(f"{name} removed.")
            await render()

        return handler

    async def on_add() -> None:
        """Add the variable from the form, clear the form and re-render the list."""
        try:
            await run.io_bound(
                svc.set_user_env, auth.user.name, user_id, name_in.value or "", value_in.value or ""
            )
        except ValidationError as exc:
            ui.notify(str(exc), type="negative")
            return
        name_in.set_value("")
        value_in.set_value("")
        ui.notify("Added.", type="positive")
        await render()

    add_btn.on_click(guarded(auth, on_add))
    ui.timer(0.1, guarded(auth, render), once=True)
