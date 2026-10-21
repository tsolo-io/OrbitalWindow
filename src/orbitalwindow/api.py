# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Plain HTTP endpoints: login/logout, password change and Traefik forwardAuth.

Login and password change are ordinary HTML form POSTs (not NiceGUI events) so the response can
set the HttpOnly session cookie and passwords never travel over the UI websocket.
"""

import re
import ssl
from enum import IntEnum
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse

from .auth import COOKIE_NAME, SessionUser
from .chat import trusted_headers
from .service import Service
from .users import ValidationError

router = APIRouter()

OWNER_URI_RE = {
    "desktop": re.compile(r"^/desktop/([a-z0-9]+)(?:/|$|\?)"),
    "files": re.compile(r"^/files/([a-z0-9]+)(?:/|$|\?)"),
    "code": re.compile(r"^/code/([a-z0-9]+)(?:/|$|\?)"),
}
# Characters allowed in a post-login target; anything else falls back to the default.
NEXT_RE = re.compile(r"^/[A-Za-z0-9/_.~%=&?-]*$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(:[0-9]{1,5})?$|^\[[0-9a-fA-F:]+\](:[0-9]{1,5})?$")


class Access(IntEnum):
    ALLOW = 200
    LOGIN = 302
    DENY = 403


def owner_access(
    user: SessionUser | None, forwarded_uri: str, kind: str = "desktop"
) -> tuple[Access, str | None]:
    """Decide forwardAuth for ``/desktop/<owner>/…``, ``/files/<owner>/…`` or ``/code/<owner>/…``.

    Only the owner gets in; admins included.

    Args:
        user: The logged-in user, or None when there is no valid session.
        forwarded_uri: The original request URI from Traefik's ``X-Forwarded-Uri``.
        kind: Which route family to match: ``"desktop"``, ``"files"`` or ``"code"``.

    Returns:
        The access decision and, when allowed, the owner's user name (otherwise None).
    """
    match = OWNER_URI_RE[kind].match(forwarded_uri or "")
    if match is None:
        return Access.DENY, None
    if user is None:
        return Access.LOGIN, None
    if user.name != match.group(1):
        return Access.DENY, None
    return Access.ALLOW, user.name


def safe_next(target: str | None, default: str = "/") -> str:
    """Only allow local absolute paths as a post-login target (no open redirects).

    Args:
        target: The requested post-login path.
        default: Path to use when ``target`` is missing or not safe.

    Returns:
        ``target`` if it is a safe local path, otherwise ``default``.
    """
    if not target or not NEXT_RE.fullmatch(target) or target.startswith("//"):
        return default
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return default
    return target


def same_origin(request: Request) -> bool:
    """Reject cross-site form posts.

    Browsers send Origin on POST; require it to match Host.

    Args:
        request: The incoming request.

    Returns:
        True if the request has an Origin header whose host matches the Host header.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return False
    return urlsplit(origin).netloc == request.headers.get("host")


def svc(request: Request) -> Service:
    """Return the application's service object.

    Args:
        request: The incoming request.

    Returns:
        The ``Service`` stored on the app state.
    """
    return request.app.state.service


def current_user(request: Request) -> SessionUser | None:
    """Return the user of the request's session cookie.

    Args:
        request: The incoming request.

    Returns:
        The session user, or None when the cookie is missing or no longer valid.
    """
    return svc(request).sessions.resolve(request.cookies.get(COOKIE_NAME))


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    """Set the HttpOnly session cookie on a response.

    Args:
        request: The incoming request (used to reach the service settings).
        response: The response to set the cookie on.
        token: The signed session token.
    """
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
    """Return the client's address.

    Args:
        request: The incoming request.

    Returns:
        The client host, or ``"unknown"`` when it is not available.
    """
    return request.client.host if request.client else "unknown"


def _redirect(url: str) -> RedirectResponse:
    """Return a 303 See Other redirect.

    Args:
        url: The redirect target.

    Returns:
        A redirect response with status 303.
    """
    return RedirectResponse(url, status_code=303)


@router.post("/auth/login")
def login(
    request: Request,
    username: Annotated[str, Form(max_length=64)] = "",
    password: Annotated[str, Form(max_length=256)] = "",
    next: Annotated[str, Form(max_length=512)] = "/",
) -> Response:
    """Handle the login form: check the credentials and set the session cookie.

    Args:
        request: The incoming request.
        username: The submitted user name.
        password: The submitted password.
        next: Where to go after a successful login.

    Returns:
        A redirect to ``next`` on success, or back to the login page with an error code.
    """
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
    """Log out everywhere.

    The session version is bumped, so tokens held by other tabs or devices stop working too
    (not just this browser's cookie).

    Args:
        request: The incoming request.

    Returns:
        A redirect to the login page.
    """
    response = _redirect("/login")
    if same_origin(request):
        user = current_user(request)
        if user is not None:
            svc(request).logout(user.id)
        response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.post("/account/password")
def change_password(
    request: Request,
    current: Annotated[str, Form(max_length=256)] = "",
    new: Annotated[str, Form(max_length=256)] = "",
    confirm: Annotated[str, Form(max_length=256)] = "",
) -> Response:
    """Handle the change-password form and re-issue the session cookie.

    Args:
        request: The incoming request.
        current: The user's current password.
        new: The new password.
        confirm: The new password again.

    Returns:
        A redirect to the home page with a ``pw`` status code in the query string.
    """
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
    except ValidationError as exc:
        if "current password" in str(exc):
            service.limiter.failure(*keys)
            return _redirect("/?pw=current")
        return _redirect("/?pw=weak")
    response = _redirect("/?pw=samba" if result.warnings else "/?pw=ok")
    set_session_cookie(request, response, service.sessions.issue(user.id, ver))
    return response


def _login_redirect(request: Request, uri: str) -> Response:
    """Redirect a forwardAuth request to the login page, coming back to ``uri`` afterwards.

    Args:
        request: The forwardAuth request from Traefik.
        uri: The original request URI to return to after login.

    Returns:
        An absolute 302 redirect to the login page, or a 401 response when the forwarded
        protocol or host is not valid.
    """
    # Traefik resolves a relative Location against the auth server, so make it absolute.
    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("x-forwarded-host", "")
    if proto not in ("http", "https") or not HOST_RE.fullmatch(host):
        return PlainTextResponse("Please log in.", status_code=401)
    return RedirectResponse(f"{proto}://{host}/login?next={quote(safe_next(uri))}", status_code=302)


@router.get("/auth/desktop")
def auth_desktop(request: Request) -> Response:
    """Traefik forwardAuth for desktop routers. Also used for their WebSocket upgrades.

    Args:
        request: The forwardAuth request from Traefik.

    Returns:
        200 for the owner, a login redirect without a session, otherwise 403.
    """
    uri = request.headers.get("x-forwarded-uri", "")
    return _owner_response(request, uri, "desktop")


@router.get("/auth/files")
def auth_files(request: Request) -> Response:
    """Traefik forwardAuth for file manager routers. ``X-OW-User`` logs the owner in there.

    Args:
        request: The forwardAuth request from Traefik.

    Returns:
        200 for the owner, a login redirect without a session, otherwise 403.
    """
    return _owner_response(request, request.headers.get("x-forwarded-uri", ""), "files")


def _owner_response(request: Request, uri: str, kind: str) -> Response:
    """Build the forwardAuth response for an owner-only route.

    Args:
        request: The forwardAuth request from Traefik.
        uri: The original request URI.
        kind: Which route family to match: ``"desktop"``, ``"files"`` or ``"code"``.

    Returns:
        200 with an ``X-OW-User`` header for the owner, a login redirect without a session,
        otherwise 403.
    """
    access, owner = owner_access(current_user(request), uri, kind)
    if access is Access.ALLOW:
        return PlainTextResponse("ok", headers={"X-OW-User": owner or ""})
    if access is Access.LOGIN:
        return _login_redirect(request, uri)
    return PlainTextResponse("Forbidden", status_code=403)


@router.get("/auth/code")
def auth_code(request: Request) -> Response:
    """Traefik forwardAuth for VS Code routers: only the owner gets in.

    Args:
        request: The forwardAuth request from Traefik.

    Returns:
        200 for the owner, a login redirect without a session, otherwise 403.
    """
    return _owner_response(request, request.headers.get("x-forwarded-uri", ""), "code")


@router.get("/auth/admin")
def auth_admin(request: Request) -> Response:
    """Traefik forwardAuth for admin tools (cAdvisor, later Dozzle): admins only.

    Args:
        request: The forwardAuth request from Traefik.

    Returns:
        200 with user headers for admins, a login redirect without a session, otherwise 403.
    """
    user = current_user(request)
    if user is None:
        return _login_redirect(request, request.headers.get("x-forwarded-uri", ""))
    if not user.is_admin:
        return PlainTextResponse("Forbidden", status_code=403)
    return PlainTextResponse("ok", headers={"X-OW-User": user.name, "Remote-User": user.name})


BENCH_FILES = {
    "samples.jsonl": "application/x-ndjson",
    "raw.jsonl": "application/x-ndjson",
    "samples_eval_results.json": "application/json",
}


@router.get("/benchmarks/{bench_id}/files/{name}")
def benchmark_file(request: Request, bench_id: int, name: str) -> Response:
    """Download a file of a benchmark (any logged-in user).

    Args:
        request: The request.
        bench_id: The benchmark.
        name: One of the files in ``BENCH_FILES``.

    Returns:
        The file, a redirect to the login page without a session, or 404.
    """
    if current_user(request) is None:
        return RedirectResponse(f"/login?next=/benchmarks/{bench_id}", status_code=303)
    path = svc(request).benchmarks.folder(bench_id) / name
    if name not in BENCH_FILES or not path.is_file():
        return PlainTextResponse("Not found", status_code=404)
    return FileResponse(
        path,
        media_type=BENCH_FILES[name],
        filename=f"benchmark-{bench_id}-{name}",
    )


@router.get("/auth/ai")
def auth_ai(request: Request) -> Response:
    """Traefik forwardAuth for the chat (Open WebUI): every logged-in user.

    Open WebUI trusts the headers to sign the user in: ``X-OW-Email`` names the account,
    ``X-OW-Name`` is the display name (URL-encoded) and ``X-OW-Role`` makes OrbitalWindow admins
    Open WebUI admins and everyone else a user. All three are always sent, so a client's own copies
    never reach Open WebUI.

    Args:
        request: The forwardAuth request from Traefik.

    Returns:
        200 with the user headers, or a redirect to OrbitalWindow's login page (on the normal
        HTTPS port, since the chat runs on its own port).
    """
    user = current_user(request)
    if user is None:
        proto = request.headers.get("x-forwarded-proto", "https")
        host = request.headers.get("x-forwarded-host", "")
        if proto not in ("http", "https") or not HOST_RE.fullmatch(host):
            return PlainTextResponse("Please log in.", status_code=401)
        hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        return RedirectResponse(f"{proto}://{hostname}/login?next=/", status_code=302)
    return PlainTextResponse(
        "ok", headers=trusted_headers(user.name, user.display_name, user.is_admin)
    )


@router.get("/orbitalwindow.crt", include_in_schema=False)
def certificate(request: Request) -> Response:
    """Serve the server's TLS certificate, for programs that should trust it (#61).

    It is the certificate Traefik presents (public: every browser receives it), read from
    Traefik's TLS handshake, so it follows a renewed or real certificate.

    Args:
        request: The incoming request.

    Returns:
        The certificate in PEM format, or 503 when Traefik can't be reached.
    """
    traefik = svc(request).settings.traefik_container
    try:
        pem = ssl.get_server_certificate((traefik, 443), timeout=5)
    except OSError:
        return PlainTextResponse("The certificate is not available right now.", status_code=503)
    return Response(
        pem,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": 'attachment; filename="orbitalwindow.crt"'},
    )


@router.get("/healthz", include_in_schema=False)
def healthz() -> Response:
    """Report that the server is up.

    Returns:
        A plain ``ok`` response.
    """
    return PlainTextResponse("ok")
