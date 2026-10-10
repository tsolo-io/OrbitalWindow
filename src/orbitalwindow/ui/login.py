# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The login page. The form posts straight to ``/auth/login``."""

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import ui

from ..api import safe_next
from ..auth import COOKIE_NAME
from .common import form_input, service

ERRORS = {
    "invalid": "The user name or password is not correct.",
    "limit": "Too many failed attempts. Please wait a few minutes and try again.",
    "origin": "Your login could not be checked. Please reload the page and try again.",
}


@ui.page("/login")
def login_page(request: Request, error: str = "", next: str = "/") -> RedirectResponse | None:
    """Render the login page, or skip it when the visitor already has a valid session.

    Args:
        request: The incoming page request.
        error: Key into ``ERRORS`` of a message to show after a failed login.
        next: Path to go to after logging in; checked with ``safe_next``.

    Returns:
        A redirect to ``next`` if already logged in, else None (the page is rendered).
    """
    target = safe_next(next)
    if service().sessions.resolve(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse(target, status_code=303)

    ui.page_title("Log in · OrbitalWindow")
    ui.colors(primary="#1f4e79")
    with ui.column().classes("absolute-center items-center gap-4"):
        ui.label("OrbitalWindow").classes("text-3xl font-bold text-primary")
        ui.label("Your research desktop in the browser").classes("text-grey-8")
        with ui.card().classes("w-80"):
            if error in ERRORS:
                ui.label(ERRORS[error]).classes("text-negative")
            with (
                ui.element("form")
                .props('method=post action="/auth/login"')
                .classes("column w-full gap-2")
            ):
                form_input("User name", "username", autocomplete="username", autofocus=True)
                form_input("Password", "password", type="password", autocomplete="current-password")
                ui.element("input").props(f'type=hidden name=next value="{target}"')
                ui.button("Log in").props("type=submit no-caps").classes("w-full")
        ui.label("Forgotten your password? Ask your administrator to reset it.").classes(
            "text-caption text-grey-7"
        )
    return None
