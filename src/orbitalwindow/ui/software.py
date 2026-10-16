# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Dashboard: the Software card (add-on switches, package editor, Apply) and build logs."""

from nicegui import run, ui

from ..users import ValidationError
from .common import PageAuth, guarded, help_icon, service


def show_build_log(build_id: int, title: str = "Installation log") -> None:
    """Open a dialog that follows a build's log while it runs.

    Args:
        build_id: Id of the build.
        title: Dialog title.
    """
    svc = service()
    with ui.dialog().props("maximized=false") as dialog, ui.card().classes("w-full max-w-4xl"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(title).classes("text-h6")
            status = ui.badge("")
        log = (
            ui.code("", language=None)
            .classes("w-full text-xs")
            .style("max-height: 60vh; overflow: auto; white-space: pre-wrap")
        )
        ui.button("Close", on_click=dialog.close).props("no-caps flat")

        async def refresh() -> None:
            """Show the build's latest log and status; stop polling once it has finished."""
            build = await run.io_bound(svc.build_log, build_id)
            if build is None:
                return
            log.set_content(build["log"][-60_000:] or "Waiting for the build to start…")
            colors = {"queued": "grey", "running": "orange", "ok": "positive", "failed": "negative"}
            labels = {"queued": "Waiting", "running": "Running", "ok": "Done", "failed": "Failed"}
            status.set_text(labels.get(build["status"], build["status"]))
            status.props(f"color={colors.get(build['status'], 'grey')}")
            if build["status"] in ("ok", "failed"):
                timer.deactivate()

        timer = ui.timer(1.5, refresh)
        ui.timer(0.05, refresh, once=True)
    dialog.on("hide", lambda: timer.deactivate())
    dialog.open()


def software_card(auth: PageAuth) -> None:
    """Add the software card: add-on switches, extra packages, Apply and the build status.

    Args:
        auth: Session of the page.
    """
    svc = service()
    user_id = auth.user.id
    actor = auth.user.name

    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-1"):
            ui.label("Software").classes("text-h6")
            help_icon("software")
        ui.label(
            "Switch on the software you need and add extra Ubuntu packages. Then press Apply: "
            "your desktop is prepared in the background, and the new software appears the next "
            "time you start it."
        ).classes("text-sm text-grey-8")

        addons_box = ui.column().classes("w-full gap-1")
        ui.separator()
        ui.label("Extra packages").classes("text-subtitle1")
        packages_box = ui.row().classes("w-full gap-1")
        with ui.row().classes("items-center gap-2"):
            pkg_input = (
                ui.input("Package name", placeholder="for example: octave")
                .props("autocapitalize=none dense outlined clearable")
                .classes("w-64")
            )
            add_btn = ui.button("Add", icon="add").props("no-caps outline")
        suggestions = ui.row().classes("w-full gap-1")
        ui.separator()
        with ui.row().classes("items-center gap-3"):
            apply_btn = ui.button("Apply", icon="build").props("no-caps")
            status = ui.label("").classes("text-sm")
            log_btn = ui.button("Show log", icon="article").props("no-caps flat dense")
        latest: dict[str, int | None] = {"build": None}

        def render_addons() -> None:
            """Rebuild the list of add-on switches."""
            addons_box.clear()
            with addons_box:
                states = svc.software.addon_states(user_id)
                if not states:
                    ui.label("No optional software has been set up yet.").classes("text-grey-7")
                for state in states:
                    spec = state.addon.spec
                    with ui.row().classes("items-start no-wrap gap-2"):
                        switch = ui.switch(value=state.active).props("dense")
                        switch.set_enabled(state.available and not state.needs_desktop)
                        with ui.column().classes("gap-0"):
                            ui.label(spec.label).classes("font-medium")
                            text = spec.description or ""
                            if state.needs_desktop:
                                text = (text + " Needs a desktop; yours is terminal only.").strip()
                            elif not state.available:
                                text = (text + " Ask your administrator for access.").strip()
                            ui.label(text).classes("text-caption text-grey-7")
                    switch.on_value_change(guarded(auth, _toggle(spec.key)))

        def _toggle(key: str):
            """Make a switch handler that switches one add-on on or off.

            Args:
                key: Add-on key.

            Returns:
                An async value-change handler.
            """

            async def handler(event) -> None:
                """Save the switch's new value; re-render the switches if it was refused.

                Args:
                    event: Value-change event of the switch.
                """
                try:
                    await run.io_bound(svc.set_addon, actor, user_id, key, bool(event.value))
                except ValidationError as exc:
                    ui.notify(str(exc), type="negative")
                    render_addons()
                await refresh_status()

            return handler

        def render_packages() -> None:
            """Rebuild the chips of the user's extra packages."""
            packages_box.clear()
            with packages_box:
                names = svc.software.packages(user_id)
                if not names:
                    ui.label("None.").classes("text-grey-7 text-sm")
                for name in names:
                    chip = ui.chip(name, removable=True).props("outline")
                    chip.on("remove", guarded(auth, _remove(name)))

        def _remove(name: str):
            """Make a chip handler that removes one extra package.

            Args:
                name: Package name.

            Returns:
                An async remove handler.
            """

            async def handler() -> None:
                """Remove the package and update the list and status."""
                await run.io_bound(svc.remove_package, actor, user_id, name)
                render_packages()
                await refresh_status()

            return handler

        async def on_type(event) -> None:
            """Suggest package names matching what has been typed so far.

            Args:
                event: Value-change event of the package input.
            """
            image_id = svc.db.scalar("SELECT image_id FROM users WHERE id = ?", [user_id])
            prefix = (event.value or "").strip()
            names = await run.io_bound(svc.software.search_packages, image_id, prefix, 12)
            suggestions.clear()
            with suggestions:
                if len(prefix) >= 2 and names and names != [prefix]:
                    ui.label("Suggestions:").classes("text-caption text-grey-7 self-center")
                    for name in names:
                        ui.chip(name, icon="add", on_click=guarded(auth, _add(name))).props(
                            "clickable dense outline color=primary"
                        )

        def _add(name: str):
            """Make a click handler that adds a suggested package.

            Args:
                name: Package name.

            Returns:
                An async click handler.
            """

            async def handler() -> None:
                """Put the suggestion into the input and add it."""
                pkg_input.set_value(name)
                await on_add()

            return handler

        async def on_add() -> None:
            """Add the package from the input and update the list and status."""
            try:
                name = await run.io_bound(svc.add_package, actor, user_id, pkg_input.value or "")
            except ValidationError as exc:
                ui.notify(str(exc), type="negative")
                return
            ui.notify(f"Added {name}. Press Apply to install it.")
            pkg_input.set_value("")
            suggestions.clear()
            render_packages()
            await refresh_status()

        async def on_apply() -> None:
            """Start building the user's software, if anything changed, and show the log."""
            try:
                build_id = await run.io_bound(svc.apply_software, actor, user_id)
            except ValidationError as exc:
                ui.notify(str(exc), type="negative")
                return
            if build_id is None:
                ui.notify("Your software is already up to date.")
            else:
                latest["build"] = build_id
                show_build_log(build_id)
            await refresh_status()

        def on_log() -> None:
            """Show the log of the latest build."""
            if latest["build"]:
                show_build_log(latest["build"])

        async def refresh_status() -> None:
            """Update the status text, the Apply button and the log button."""
            row = await run.io_bound(svc.software.user_image, user_id)
            pending = await run.io_bound(svc.software_pending, user_id)
            latest["build"] = row["build_id"] if row else None
            building = bool(row and row["status"] in ("queued", "building"))
            failed = bool(row and row["status"] == "failed")
            if building:
                text, cls = "Installing your software…", "text-orange-9"
            elif failed and pending:
                text, cls = "The last installation failed. See the log.", "text-negative"
            elif pending:
                text, cls = "You have changes that are not installed yet.", "text-orange-9"
            else:
                text, cls = "Your software is up to date.", "text-positive"
            status.set_text(text)
            status.classes(replace=f"text-sm {cls}")
            apply_btn.set_enabled(pending and not building)
            log_btn.set_visibility(latest["build"] is not None)

        pkg_input.on_value_change(guarded(auth, on_type))
        pkg_input.on("keydown.enter", guarded(auth, on_add))
        add_btn.on_click(guarded(auth, on_add))
        apply_btn.on_click(guarded(auth, on_apply))
        log_btn.on_click(guarded(auth, on_log))
        render_addons()
        render_packages()
        ui.timer(3.0, guarded(auth, refresh_status))
        ui.timer(0.1, guarded(auth, refresh_status), once=True)
