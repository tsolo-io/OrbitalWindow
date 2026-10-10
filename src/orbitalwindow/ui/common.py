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
    """Register the service instance the pages use.

    Args:
        service: The application service, set once at startup.
    """
    global _service
    _service = service


def service() -> Service:
    """Return the service instance registered with ``set_service``.

    Returns:
        The application service.
    """
    assert _service is not None, "service not initialised"
    return _service


class PageAuth:
    """The session a page was opened with.

    Event handlers and timers call ``check`` before acting, so logging out, a password change, a
    disabled account or lost admin (or AI manager) rights take effect on open pages without a
    reload.
    """

    def __init__(
        self, token: str, user: SessionUser, *, admin: bool = False, ai: bool = False
    ) -> None:
        """Remember the session a page was opened with.

        Args:
            token: Session token from the request cookie.
            user: The user the token resolved to when the page opened.
            admin: Whether the page needs admin rights.
            ai: Whether the page needs AI manager rights (admins have them too).
        """
        self._token = token
        self.user = user
        self.admin = admin  # the page needs admin rights
        self.ai = ai  # the page needs AI manager rights

    def check(self) -> SessionUser | None:
        """Re-check the session; navigate away if it is no longer valid for this page.

        Goes to the login page when the session has ended, or to the dashboard when an admin (or
        AI) page lost its rights.

        Returns:
            The current session user, or None if the page was navigated away.
        """
        user = service().sessions.resolve(self._token)
        if user is None:
            ui.navigate.to("/login")
            return None
        if (self.admin and not user.is_admin) or (self.ai and not user.can_manage_ai):
            ui.navigate.to("/")
            return None
        return user


def authenticate(
    request: Request, *, admin: bool = False, ai: bool = False
) -> PageAuth | RedirectResponse:
    """Check the session cookie of a page request.

    Args:
        request: The incoming page request.
        admin: Whether the page needs admin rights.
        ai: Whether the page needs AI manager rights (admins have them too).

    Returns:
        A ``PageAuth`` for the session, or a redirect to the login page (no valid session) or to
        the dashboard (missing admin or AI manager rights).
    """
    token = request.cookies.get(COOKIE_NAME, "")
    user = service().sessions.resolve(token)
    if user is None:
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)
    if (admin and not user.is_admin) or (ai and not user.can_manage_ai):
        return RedirectResponse("/", status_code=303)
    return PageAuth(token, user, admin=admin, ai=ai)


def post_button(label: str, action: str, *, icon: str | None = None, flat: bool = True) -> None:
    """Add a button that submits a plain form POST (used for log out).

    Args:
        label: Button text.
        action: URL the form posts to.
        icon: Optional icon name.
        flat: Whether to style the button flat and white (for the header).
    """
    with ui.element("form").props(f'method=post action="{action}"'):
        btn = ui.button(label, icon=icon).props("type=submit no-caps")
        if flat:
            btn.props("flat color=white")


def frame(auth: PageAuth, title: str) -> None:
    """Add the header with navigation. Call at the top of every logged-in page.

    Args:
        auth: Session of the page; admins also get the admin links.
        title: Page title shown in the browser tab.
    """
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
            if auth.user.can_manage_ai:
                ui.link("AI", "/ai").classes("text-white no-underline")
        with ui.row().classes("items-center gap-2"):
            ui.link("Server status", "/status").classes("text-white no-underline mr-2")
            ui.link("Help", "/help").classes("text-white no-underline mr-2")
            ui.label(auth.user.display_name).classes("text-white")
            post_button("Log out", "/auth/logout", icon="logout")


def guarded(auth: PageAuth, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap an event handler or timer callback so it re-checks the session first.

    The wrapper re-checks the session (and admin rights on admin pages), then runs the callback.
    Works for sync and async callbacks.

    Args:
        auth: Session of the page.
        fn: The handler or callback to wrap.

    Returns:
        An async callback taking an event argument if ``fn`` declares a parameter, else none.
    """

    async def call(*args: Any) -> Any:
        """Run ``fn`` if the session is still valid, awaiting its result if needed.

        Args:
            *args: Arguments passed on to ``fn`` (the event, if any).

        Returns:
            The result of ``fn``, or None if the session check failed.
        """
        if auth.check() is None:
            return None
        result = fn(*args)
        if inspect.isawaitable(result):
            result = await result
        return result

    if inspect.signature(fn).parameters:
        # NiceGUI passes the event only to handlers that declare a parameter.
        async def with_event(event: Any) -> Any:
            """Run the guarded handler with the NiceGUI event.

            Args:
                event: The NiceGUI event.

            Returns:
                The result of the handler.
            """
            return await call(event)

        return with_event

    async def without_event() -> Any:
        """Run the guarded handler without arguments.

        Returns:
            The result of the handler.
        """
        return await call()

    return without_event


def form_input(
    label: str, name: str, *, type: str = "text", autocomplete: str = "off", autofocus: bool = False
) -> None:
    """Add a labelled plain HTML input for forms that POST to the server.

    NiceGUI inputs send every keystroke over the UI websocket; these don't, so passwords only
    leave the browser in the form submission.

    Args:
        label: Text shown above the input.
        name: Form field name.
        type: HTML input type.
        autocomplete: HTML autocomplete hint.
        autofocus: Whether the input gets the focus when the page loads.
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
    """Add a small "?" button that opens the matching user guide page in a new tab.

    Args:
        slug: Slug of the help page, as in ``/help/<slug>``.
    """
    ui.button(icon="help_outline", on_click=lambda: ui.navigate.to(f"/help/{slug}", new_tab=True))\
        .props("flat round dense size=sm color=grey-7").tooltip("Help")  # fmt: skip
