# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Shared page pieces: the service handle, session checks and the page frame."""

import inspect
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import ui

from ..auth import COOKIE_NAME, SessionUser
from ..service import Service

_service: Service | None = None


def set_service(service: Service) -> None:
    global _service
    _service = service


def service() -> Service:
    assert _service is not None, "service not initialised"
    return _service


class PageAuth:
    """The session a page was opened with. Event handlers and timers call ``check`` before acting,
    so logging out, a password change, a disabled account or lost admin rights take effect on open
    pages without a reload."""

    def __init__(self, token: str, user: SessionUser, *, admin: bool = False) -> None:
        self._token = token
        self.user = user
        self.admin = admin  # the page needs admin rights

    def check(self) -> SessionUser | None:
        user = service().sessions.resolve(self._token)
        if user is None:
            ui.navigate.to("/login")
            return None
        if self.admin and not user.is_admin:
            ui.navigate.to("/")
            return None
        return user


def authenticate(request: Request, *, admin: bool = False) -> PageAuth | RedirectResponse:
    token = request.cookies.get(COOKIE_NAME, "")
    user = service().sessions.resolve(token)
    if user is None:
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)
    if admin and not user.is_admin:
        return RedirectResponse("/", status_code=303)
    return PageAuth(token, user, admin=admin)


def post_button(label: str, action: str, *, icon: str | None = None, flat: bool = True) -> None:
    """A button that submits a plain form POST (used for log out)."""
    with ui.element("form").props(f'method=post action="{action}"'):
        btn = ui.button(label, icon=icon).props("type=submit no-caps")
        if flat:
            btn.props("flat color=white")


def frame(auth: PageAuth, title: str) -> None:
    """Header with navigation. Call at the top of every logged-in page."""
    ui.page_title(f"{title} · OrbitalWindow")
    ui.colors(primary="#1f4e79")
    with ui.header().classes("items-center justify-between px-4 py-2"):
        with ui.row().classes("items-center gap-4"):
            ui.link("OrbitalWindow", "/").classes("text-lg font-bold text-white no-underline")
            ui.link("My desktop", "/").classes("text-white no-underline")
            if auth.user.is_admin:
                for text, url in (
                    ("Users", "/admin/users"),
                    ("Desktops", "/admin/images"),
                    ("Add-ons", "/admin/addons"),
                    ("Groups", "/admin/groups"),
                    ("Settings", "/admin/settings"),
                    ("System", "/admin/system"),
                ):
                    ui.link(text, url).classes("text-white no-underline")
        with ui.row().classes("items-center gap-2"):
            ui.link("Help", "/help").classes("text-white no-underline mr-2")
            ui.label(auth.user.display_name).classes("text-white")
            post_button("Log out", "/auth/logout", icon="logout")


def guarded(auth: PageAuth, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap an event handler or timer callback: re-check the session (and admin rights on admin
    pages), then run it. Works for sync and async callbacks."""

    async def call(*args: Any) -> Any:
        if auth.check() is None:
            return None
        result = fn(*args)
        if inspect.isawaitable(result):
            result = await result
        return result

    if inspect.signature(fn).parameters:
        # NiceGUI passes the event only to handlers that declare a parameter.
        async def with_event(e: Any) -> Any:
            return await call(e)

        return with_event

    async def without_event() -> Any:
        return await call()

    return without_event


def form_input(
    label: str, name: str, *, type: str = "text", autocomplete: str = "off", autofocus: bool = False
) -> None:
    """A plain HTML input for forms that POST to the server.

    NiceGUI inputs send every keystroke over the UI websocket; these don't, so passwords only
    leave the browser in the form submission.
    """
    with ui.element("label").classes("column w-full gap-1"):
        ui.label(label).classes("text-sm text-grey-8")
        props = f"type={type} name={name} autocomplete={autocomplete} required"
        if autofocus:
            props += " autofocus"
        ui.element("input").props(props).classes(
            "w-full rounded border border-grey-5 px-3 py-2 text-base"
        )


def help_icon(slug: str) -> None:
    """A small "?" that opens the matching user guide page in a new tab."""
    ui.button(icon="help_outline", on_click=lambda: ui.navigate.to(f"/help/{slug}", new_tab=True))\
        .props("flat round dense size=sm color=grey-7").tooltip("Help")  # fmt: skip
