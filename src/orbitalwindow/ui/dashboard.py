# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The user dashboard: desktop status, start/stop/open, desktop choice and password change."""

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import run, ui

from ..desktops import DesktopError, DesktopState, desktop_path
from ..files import files_path
from ..metrics import fmt_bytes
from ..users import ValidationError
from .common import PageAuth, authenticate, form_input, frame, guarded, help_icon, service
from .software import software_card

STATE_TEXT = {
    DesktopState.STOPPED: ("Stopped", "grey"),
    DesktopState.BUILDING: ("Preparing your desktop…", "orange"),
    DesktopState.STARTING: ("Starting…", "orange"),
    DesktopState.RUNNING: ("Running", "positive"),
    DesktopState.ERROR: ("Stopped with an error. Press Start to try again.", "negative"),
}

PW_MESSAGES = {
    "ok": ("Your password has been changed.", "positive"),
    "samba": (
        "Your password has been changed, but Windows file sharing was not updated. "
        "Please tell your administrator.",
        "warning",
    ),
    "current": ("The current password is not correct.", "negative"),
    "mismatch": ("The two new passwords are not the same.", "negative"),
    "weak": ("The new password is too short or not allowed.", "negative"),
    "limit": ("Too many attempts. Please wait a few minutes.", "negative"),
    "origin": (
        "The change could not be checked. Please reload the page and try again.",
        "negative",
    ),
}


@ui.page("/")
def dashboard(request: Request, pw: str = "") -> RedirectResponse | None:
    auth = authenticate(request)
    if isinstance(auth, RedirectResponse):
        return auth
    frame(auth, "My desktop")
    if pw in PW_MESSAGES:
        text, kind = PW_MESSAGES[pw]
        ui.notify(text, type=kind, timeout=8000)
    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-4"):
        desktop_card(auth)
        software_card(auth)
        ssh_card(auth)
        files_card(auth)
        password_card()
    return None


def desktop_card(auth: PageAuth) -> None:
    svc = service()
    user_id = auth.user.id
    open_url = desktop_path(auth.user.name)

    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-1"):
            ui.label("My desktop").classes("text-h6")
            help_icon("getting-started")
        with ui.row().classes("items-center gap-2"):
            ui.label("Status:")
            status = ui.badge("…")
        admin_note = ui.label("Your desktop was stopped by an administrator.").classes(
            "text-caption text-grey-8"
        )
        restart_note = ui.label(
            "New software is ready. Stop and start your desktop to use it."
        ).classes("text-caption text-orange-9")
        images = {i["id"]: i["label"] for i in svc.enabled_images()}
        picker = ui.select(images, label="Desktop").classes("w-64")
        ui.label("You can change the desktop while it is stopped.").classes(
            "text-caption text-grey-7"
        )
        with ui.row().classes("gap-2"):
            start_btn = ui.button("Start", icon="play_arrow").props("no-caps")
            stop_btn = ui.button("Stop", icon="stop").props("no-caps outline")
            open_btn = ui.button(
                "Open desktop",
                icon="open_in_new",
                on_click=lambda: ui.navigate.to(open_url, new_tab=True),
            ).props("no-caps color=positive")
        usage_box = ui.column().classes("w-full gap-1")
        busy = {"value": False}

        async def show_usage(running: bool) -> None:
            use = await run.io_bound(svc.usage_for, auth.user.name) if running else None
            gpus = await run.io_bound(svc.gpus.stats)
            usage_box.clear()
            with usage_box:
                if use:
                    mem_gb = svc.db.scalar("SELECT mem_gb FROM users WHERE id = ?", [user_id])
                    limit = f"{mem_gb:g} GB" if mem_gb else "none"
                    ui.label(
                        f"Your desktop uses {use.cpu_cores:.1f} CPU cores and "
                        f"{fmt_bytes(use.mem_bytes)} of RAM (limit: {limit})."
                    ).classes("text-sm text-grey-8")
                for g in gpus:
                    ui.label(
                        f"GPU {g.index} ({g.name}), shared by everyone: {g.util_percent}% busy, "
                        f"{fmt_bytes(g.mem_used)} of {fmt_bytes(g.mem_total)} memory in use."
                    ).classes("text-sm text-grey-8")

        async def refresh() -> None:
            user = await run.io_bound(svc.db.one, "SELECT * FROM users WHERE id = ?", [user_id])
            if user is None:
                return
            try:
                state = await run.io_bound(svc.desktop_state, user_id)
            except Exception:
                state = DesktopState.ERROR
            text, color = STATE_TEXT[state]
            status.set_text(text)
            status.props(f"color={color}")
            running = state in (DesktopState.RUNNING, DesktopState.STARTING)
            start_btn.set_visibility(not running)
            stop_btn.set_visibility(running)
            open_btn.set_enabled(state is DesktopState.RUNNING)
            start_btn.set_enabled(not busy["value"] and state is not DesktopState.BUILDING)
            stop_btn.set_enabled(not busy["value"])
            picker.set_enabled(not running and not busy["value"])
            admin_note.set_visibility(state is DesktopState.STOPPED and user["stopped_by_admin"])
            restart_note.set_visibility(
                state is DesktopState.RUNNING and await run.io_bound(svc.restart_needed, user_id)
            )
            await show_usage(state is DesktopState.RUNNING)
            if picker.value != user["image_id"]:
                picker.set_value(user["image_id"])

        async def act(label: str, fn, *args) -> None:
            busy["value"] = True
            await refresh()
            try:
                await run.io_bound(fn, *args)
            except (DesktopError, ValidationError) as e:
                ui.notify(str(e), type="negative")
            except Exception as e:
                ui.notify(
                    f"{label} failed. Please try again or tell your administrator.", type="negative"
                )
                raise e
            finally:
                busy["value"] = False
                await refresh()

        async def on_start() -> None:
            await act("Start", svc.start_desktop, auth.user.name, user_id)

        async def on_stop() -> None:
            await act("Stop", svc.stop_desktop, auth.user.name, user_id)

        async def on_pick(e) -> None:
            if e.value is None:
                return
            current = svc.db.scalar("SELECT image_id FROM users WHERE id = ?", [user_id])
            if e.value != current:
                await act(
                    "Changing the desktop", svc.choose_image, auth.user.name, user_id, e.value
                )

        start_btn.on_click(guarded(auth, on_start))
        stop_btn.on_click(guarded(auth, on_stop))
        picker.on_value_change(guarded(auth, on_pick))
        ui.timer(3.0, refresh)
        ui.timer(0.1, refresh, once=True)


def files_card(auth: PageAuth) -> None:
    svc = service()
    host = svc.public_hostname()
    user_id = auth.user.id
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.row().classes("items-center gap-1"):
                ui.label("My files").classes("text-h6")
                help_icon("files")
            ui.button(
                "Open files",
                icon="folder_open",
                on_click=lambda: ui.navigate.to(files_path(auth.user.name), new_tab=True),
            ).props("no-caps outline")
        ui.label(
            "Your files are the same everywhere: in the desktop, in the web file manager "
            "(Open files) and in Windows Explorer."
        ).classes("text-sm text-grey-8")
        usage_box = ui.column().classes("w-full gap-1")

        def place(title: str, desktop: str, share: str, note: str = "") -> None:
            with ui.row().classes("w-full items-start no-wrap gap-4"):
                ui.label(title).classes("w-40 font-medium")
                with ui.column().classes("gap-0"):
                    ui.label(f"In the desktop: {desktop}").classes("text-sm")
                    ui.label(f"Windows Explorer: \\\\{host}\\{share}").classes("text-sm")
                    if note:
                        ui.label(note).classes("text-caption text-grey-7")

        ui.separator()
        place("Home folder", "/config (your home)", auth.user.name)
        groups = svc.groups_of(user_id)
        for g in groups:
            note = g["description"] or "Shared with the other members of this group."
            place(f"Group {g['name']}", f"/groups/{g['name']}", g["name"], note)
        days = svc.db.get_setting("scratch_retention_days") or "30"
        place(
            "Scratch",
            "/scratch",
            "scratch",
            f"Shared with everyone. Files untouched for {days} days are deleted automatically.",
        )
        ui.label("Use your OrbitalWindow user name and password when Windows asks.").classes(
            "text-caption text-grey-7"
        )
        group_note = ui.label(
            "Your groups have changed. Stop and start your desktop to see the new group folders."
        ).classes("text-caption text-orange-9")

        async def refresh() -> None:
            usage = await run.io_bound(svc.disk_usage_for, user_id)
            group_note.set_visibility(await run.io_bound(svc.group_restart_needed, user_id))
            usage_box.clear()
            with usage_box:
                used = fmt_bytes(usage.bytes)
                if usage.quota_gb:
                    ui.label(f"Disk use: {used} of {usage.quota_gb:g} GB").classes("text-sm")
                    ui.linear_progress(min(usage.fraction or 0, 1), show_value=False).props(
                        f"color={'negative' if usage.warning else 'primary'}"
                    )
                else:
                    ui.label(f"Disk use: {used}").classes("text-sm")
                if usage.warning:
                    ui.label(
                        "Your home folder is nearly full. To free space, empty the Trash and "
                        "remove ~/proot-apps and ~/.conda/pkgs if you don't need them."
                    ).classes("text-sm text-negative")
                if usage.measured_at is None:
                    ui.label("Disk use is measured every hour.").classes("text-caption text-grey-7")

        ui.timer(0.1, refresh, once=True)
        ui.timer(60.0, refresh)


def ssh_card(auth: PageAuth) -> None:
    """SSH keys and how to connect; only shown while the ssh add-on is switched on."""
    svc = service()
    user_id = auth.user.id
    if not any(a.key == "ssh" for a in svc.software.active_addons(user_id)):
        return
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-1"):
            ui.label("SSH keys").classes("text-h6")
            help_icon("ssh")
        ui.label("Connect to your running desktop with:").classes("text-sm")
        ui.code(svc.ssh_command(auth.user.name), language=None).classes("text-sm")
        keys_box = ui.column().classes("w-full gap-1")
        restart_note = ui.label(
            "Your keys have changed. Stop and start your desktop for the change to take effect."
        ).classes("text-caption text-orange-9")
        with ui.expansion("Add a key", icon="add").classes("w-full"):
            key_text = (
                ui.textarea("Public key", placeholder="ssh-ed25519 AAAA… you@laptop")
                .props("outlined autogrow")
                .classes("w-full")
            )
            key_label = ui.input("Name (optional)", placeholder="My laptop")
            ui.label(
                "Paste the contents of your id_ed25519.pub file — the file that ends in .pub."
            ).classes("text-caption text-grey-7")
            add_btn = ui.button("Add key").props("no-caps")

        def render_keys() -> None:
            keys_box.clear()
            with keys_box:
                keys = svc.ssh_keys(user_id)
                if not keys:
                    ui.label("No keys yet. Add the public key of each computer you use.").classes(
                        "text-sm text-grey-7"
                    )
                for k in keys:
                    with ui.row().classes("w-full items-center no-wrap gap-2"):
                        ui.icon("key")
                        with ui.column().classes("gap-0 grow"):
                            ui.label(k["label"]).classes("font-medium")
                            ui.label(k["fingerprint"]).classes("text-caption text-grey-7")
                        ui.button(icon="delete", on_click=guarded(auth, _remove(k["id"])))\
                            .props("flat round dense color=negative")  # fmt: skip
            restart_note.set_visibility(svc.ssh_restart_needed(user_id))

        def _remove(key_id: int):
            async def handler() -> None:
                await run.io_bound(svc.remove_ssh_key, auth.user.name, user_id, key_id)
                render_keys()

            return handler

        async def on_add() -> None:
            try:
                await run.io_bound(
                    svc.add_ssh_key, auth.user.name, user_id, key_label.value or "",
                    key_text.value or "",
                )  # fmt: skip
            except ValidationError as e:
                ui.notify(str(e), type="negative")
                return
            key_text.set_value("")
            key_label.set_value("")
            ui.notify("Key added.", type="positive")
            render_keys()

        add_btn.on_click(guarded(auth, on_add))
        render_keys()


def password_card() -> None:
    minimum = service().settings.min_password_length
    with (
        ui.expansion("Change my password", icon="key").classes("w-full bg-white"),
        ui.element("form")
        .props('method=post action="/account/password"')
        .classes("column gap-2 p-2 w-80"),
    ):
        form_input("Current password", "current", type="password", autocomplete="current-password")
        form_input("New password", "new", type="password", autocomplete="new-password")
        form_input("New password again", "confirm", type="password", autocomplete="new-password")
        ui.label(
            f"At least {minimum} characters. This also changes your Windows file sharing "
            "password. You will be signed out on your other devices."
        ).classes("text-caption text-grey-7")
        ui.button("Change password").props("type=submit no-caps")


@ui.page("/desktop/{name}")
@ui.page("/desktop/{name}/{rest:path}")
def desktop_not_running(request: Request, name: str, rest: str = "") -> RedirectResponse | None:
    """Reached only when no desktop router matches, i.e. the desktop is not running."""
    auth = authenticate(request)
    if isinstance(auth, RedirectResponse):
        return auth
    frame(auth, "Desktop not running")
    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-4"), ui.card().classes("w-full"):
        if name == auth.user.name:
            ui.label("Your desktop is not running.").classes("text-h6")
            ui.label("Start it from your dashboard, then press Open desktop.")
        else:
            ui.label("This desktop is not available.").classes("text-h6")
        ui.button("Go to my dashboard", on_click=lambda: ui.navigate.to("/")).props("no-caps")
    return None
