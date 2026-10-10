# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Plain HTTP endpoints: login/logout, password change and Traefik forwardAuth.

Login and password change are ordinary HTML form POSTs (not NiceGUI events) so the response can
set the HttpOnly session cookie and passwords never travel over the UI websocket.
"""

import re
from enum import IntEnum
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse

from .auth import COOKIE_NAME, SessionUser
from .service import Service
from .users import ValidationError

router = APIRouter()

DESKTOP_URI_RE = re.compile(r"^/desktop/([a-z0-9]+)(?:/|$|\?)")
# Characters allowed in a post-login target; anything else falls back to the default.
NEXT_RE = re.compile(r"^/[A-Za-z0-9/_.~%=&?-]*$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(:[0-9]{1,5})?$|^\[[0-9a-fA-F:]+\](:[0-9]{1,5})?$")


class Access(IntEnum):
    ALLOW = 200
    LOGIN = 302
    DENY = 403


def desktop_access(user: SessionUser | None, forwarded_uri: str) -> tuple[Access, str | None]:
    """Decide forwardAuth for ``/desktop/<owner>/…``. Only the owner gets in; admins included."""
    match = DESKTOP_URI_RE.match(forwarded_uri or "")
    if match is None:
        return Access.DENY, None
    if user is None:
        return Access.LOGIN, None
    if user.name != match.group(1):
        return Access.DENY, None
    return Access.ALLOW, user.name


def safe_next(target: str | None, default: str = "/") -> str:
    """Only allow local absolute paths as a post-login target (no open redirects)."""
    if not target or not NEXT_RE.fullmatch(target) or target.startswith("//"):
        return default
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return default
    return target


def same_origin(request: Request) -> bool:
    """Reject cross-site form posts. Browsers send Origin on POST; require it to match Host."""
    origin = request.headers.get("origin")
    if origin is None:
        return False
    return urlsplit(origin).netloc == request.headers.get("host")


def svc(request: Request) -> Service:
    return request.app.state.service


def current_user(request: Request) -> SessionUser | None:
    return svc(request).sessions.resolve(request.cookies.get(COOKIE_NAME))


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    service = svc(request)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=service.sessions.max_age,
        path="/",
        httponly=True,
        secure=service.settings.cookie_secure,
        samesite="lax",
    )


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@router.post("/auth/login")
def login(
    request: Request,
    username: Annotated[str, Form(max_length=64)] = "",
    password: Annotated[str, Form(max_length=256)] = "",
    next: Annotated[str, Form(max_length=512)] = "/",
) -> Response:
    target = safe_next(next)
    if not same_origin(request):
        return _redirect("/login?error=origin")
    try:
        token = svc(request).login(username, password, client_ip(request))
    except PermissionError:
        return _redirect(f"/login?error=limit&next={quote(target)}")
    if token is None:
        return _redirect(f"/login?error=invalid&next={quote(target)}")
    response = _redirect(target)
    set_session_cookie(request, response, token)
    return response


@router.post("/auth/logout")
def logout(request: Request) -> Response:
    response = _redirect("/login")
    if same_origin(request):
        response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.post("/account/password")
def change_password(
    request: Request,
    current: Annotated[str, Form(max_length=256)] = "",
    new: Annotated[str, Form(max_length=256)] = "",
    confirm: Annotated[str, Form(max_length=256)] = "",
) -> Response:
    user = current_user(request)
    if user is None:
        return _redirect("/login")
    if not same_origin(request):
        return _redirect("/?pw=origin")
    if new != confirm:
        return _redirect("/?pw=mismatch")
    service = svc(request)
    keys = (f"ip:{client_ip(request)}", f"user:{user.name}")
    if service.limiter.blocked(*keys):
        return _redirect("/?pw=limit")
    try:
        ver, result = service.change_own_password(user.id, current, new)
    except ValidationError as e:
        if "current password" in str(e):
            service.limiter.failure(*keys)
            return _redirect("/?pw=current")
        return _redirect("/?pw=weak")
    response = _redirect("/?pw=samba" if result.warnings else "/?pw=ok")
    set_session_cookie(request, response, service.sessions.issue(user.id, ver))
    return response


def _login_redirect(request: Request, uri: str) -> Response:
    # Traefik resolves a relative Location against the auth server, so make it absolute.
    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("x-forwarded-host", "")
    if proto not in ("http", "https") or not HOST_RE.fullmatch(host):
        return PlainTextResponse("Please log in.", status_code=401)
    return RedirectResponse(f"{proto}://{host}/login?next={quote(safe_next(uri))}", status_code=302)


@router.get("/auth/desktop")
def auth_desktop(request: Request) -> Response:
    """Traefik forwardAuth for desktop routers. Also used for their WebSocket upgrades."""
    uri = request.headers.get("x-forwarded-uri", "")
    access, owner = desktop_access(current_user(request), uri)
    if access is Access.ALLOW:
        return PlainTextResponse("ok", headers={"X-OW-User": owner or ""})
    if access is Access.LOGIN:
        return _login_redirect(request, uri)
    return PlainTextResponse("Forbidden", status_code=403)


@router.get("/auth/admin")
def auth_admin(request: Request) -> Response:
    """Traefik forwardAuth for admin tools (cAdvisor, later Dozzle): admins only."""
    user = current_user(request)
    if user is None:
        return _login_redirect(request, request.headers.get("x-forwarded-uri", ""))
    if not user.is_admin:
        return PlainTextResponse("Forbidden", status_code=403)
    return PlainTextResponse("ok", headers={"X-OW-User": user.name, "Remote-User": user.name})


@router.get("/healthz", include_in_schema=False)
def healthz() -> Response:
    return PlainTextResponse("ok")
