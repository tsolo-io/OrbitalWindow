# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The terminal page: the terminal in the user's own running desktop, in the browser (ADR 0030)."""

import asyncio
import codecs
import inspect

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import background_tasks, run, ui

from ..desktops import DesktopError
from ..terminal import Terminal
from .common import PageAuth, authenticate, frame, guarded, help_icon, service

OPTIONS = {"cursorBlink": True, "fontSize": 14, "scrollback": 5000}
KEEPS_RUNNING = (
    "The terminal keeps running when you close this page, so long calculations carry on. "
    "Open it again from your dashboard. Stop terminal ends it and everything running in it."
)
PLAIN_SHELL = (
    "This desktop can't keep a terminal running yet: closing this page stops what runs in it. "
    "Ask your administrator to rebuild the desktop."
)


def confirm_stop_terminal(auth: PageAuth, on_stopped) -> None:
    """Ask before stopping the user's terminal, then stop it.

    Args:
        auth: Session of the page.
        on_stopped: Called (and awaited if async) after the terminal has stopped.
    """
    svc = service()

    async def stop() -> None:
        """Stop the terminal and close the dialog."""
        dialog.close()
        await run.io_bound(svc.stop_terminal, auth.user.name, auth.user.id)
        ui.notify("The terminal has stopped.")
        result = on_stopped()
        if inspect.isawaitable(result):
            await result

    with ui.dialog() as dialog, ui.card():
        ui.label("Stop the terminal?").classes("text-h6")
        ui.label("Programs still running in it will be stopped.")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            ui.button("Stop terminal", on_click=guarded(auth, stop)).props("no-caps color=negative")
    dialog.open()


@ui.page("/terminal")
def terminal_page(request: Request) -> RedirectResponse | None:
    """Render the terminal in the user's desktop.

    Connects when the page connects and disconnects when the page closes; the terminal itself
    keeps running. There is no user name in the address: the page only ever opens the logged-in
    user's own desktop.

    Args:
        request: The incoming page request.

    Returns:
        A redirect to the login page if not logged in, else None (the page is rendered).
    """
    auth = authenticate(request)
    if isinstance(auth, RedirectResponse):
        return auth
    svc = service()
    frame(auth, "Terminal")
    current: dict[str, Terminal | None] = {"terminal": None}
    tasks: set[asyncio.Task] = set()

    with ui.column().classes("w-full p-2 gap-2").style("height: calc(100vh - 56px)"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.label("Terminal in my desktop").classes("text-h6")
            help_icon("terminal")
            start_btn = ui.button("Start a new terminal", icon="play_arrow").props(
                "no-caps outline dense"
            )
            stop_btn = ui.button("Stop terminal", icon="stop").props(
                "no-caps outline dense color=negative"
            )
            start_btn.set_visibility(False)
            stop_btn.set_visibility(False)
        note = ui.label().classes("text-caption text-grey-8")
        term = ui.xterm(OPTIONS).classes("w-full grow bg-black p-1")

    def disconnect() -> None:
        """Disconnect from the terminal, if connected, without waiting."""
        terminal, current["terminal"] = current["terminal"], None
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()
        tasks.clear()
        if terminal is not None:
            background_tasks.create(run.io_bound(terminal.close), name="terminal-close")

    def ended(message: str) -> None:
        """Show that there is no terminal any more, and offer a new one.

        Args:
            message: What happened, written into the terminal.
        """
        if start_btn.visible:  # already shown (the shell ended while it was being stopped)
            return
        disconnect()
        term.writeln(f"\r\n\r\n{message}")
        start_btn.set_visibility(True)
        stop_btn.set_visibility(False)

    async def pump(terminal: Terminal) -> None:
        """Copy the terminal's output to the page until it ends.

        Args:
            terminal: The open connection.
        """
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while data := await terminal.read():
            term.write(decoder.decode(data))
        if current["terminal"] is terminal:  # ended by itself (exit, or the desktop stopped)
            ended("The terminal has ended.")

    async def connect() -> None:
        """Connect to the terminal (starting it if needed), sized to the page."""
        start_btn.set_visibility(False)
        # Fit the terminal to the window whenever the window changes size.
        ui.run_javascript(f"window.onresize = () => getElement({term.id})?.fit()")
        await term.fit()
        rows, cols = await term.get_rows(), await term.get_columns()
        try:
            terminal = await run.io_bound(
                svc.open_terminal, auth.user.name, auth.user.id, rows, cols
            )
        except DesktopError as exc:
            term.writeln(str(exc))
            start_btn.set_visibility(True)
            return
        if terminal is None:  # the app is stopping
            return
        current["terminal"] = terminal
        note.set_text(KEEPS_RUNNING if terminal.tmux else PLAIN_SHELL)
        stop_btn.set_visibility(terminal.tmux)
        tasks.add(background_tasks.create(pump(terminal), name="terminal-pump"))
        term.run_terminal_method("focus")

    async def on_start() -> None:
        """Clear the screen and start a new terminal."""
        disconnect()
        term.run_terminal_method("reset")
        await connect()

    def on_stop() -> None:
        """Ask, then stop the terminal and everything running in it."""
        confirm_stop_terminal(auth, lambda: ended("You stopped the terminal."))

    async def on_data(event) -> None:
        """Send what the user typed to the terminal.

        Args:
            event: Xterm data event with the typed text.
        """
        terminal = current["terminal"]
        if terminal is not None:
            await terminal.write(event.data.encode())

    async def on_resize(event) -> None:
        """Tell the terminal its new size.

        Args:
            event: Xterm resize event with the new rows and columns.
        """
        terminal = current["terminal"]
        if terminal is not None:
            await run.io_bound(terminal.resize, event.rows, event.cols)

    term.on_data(guarded(auth, on_data))
    term.on_resize(on_resize)
    start_btn.on_click(guarded(auth, on_start))
    stop_btn.on_click(guarded(auth, on_stop))
    ui.context.client.on_delete(disconnect)
    ui.timer(0.2, guarded(auth, connect), once=True)
    # Leaves the page (and so disconnects) when the user logs out or is disabled.
    ui.timer(10.0, guarded(auth, lambda: None))
    return None
