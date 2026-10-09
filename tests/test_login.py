# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import pytest
from conftest import ORIGIN, login

from orbitalwindow.api import safe_next


def post_login(client, name, password, origin=ORIGIN, next="/"):
    headers = {"Origin": origin} if origin else {}
    return client.post(
        "/auth/login",
        data={"username": name, "password": password, "next": next},
        headers=headers,
        follow_redirects=False,
    )


def test_login_sets_hardened_cookie(client, users):
    r = post_login(client, "user1", "correct-horse-1", next="/desktop/user1/")
    assert r.status_code == 303
    assert r.headers["location"] == "/desktop/user1/"
    cookie = r.headers["set-cookie"].lower()
    for flag in ("httponly", "secure", "samesite=lax", "path=/", "max-age=43200"):
        assert flag in cookie


def test_login_is_case_insensitive_on_name(client, users):
    assert login(client, " User1 ") is not None


def test_wrong_password(client, users):
    r = post_login(client, "user1", "nope")
    assert r.headers["location"].startswith("/login?error=invalid")
    assert "set-cookie" not in r.headers


def test_unknown_user(client, users):
    r = post_login(client, "ghost", "whatever-123")
    assert r.headers["location"].startswith("/login?error=invalid")


@pytest.mark.parametrize("origin", [None, "https://evil.example"])
def test_cross_site_login_is_refused(client, users, origin):
    r = post_login(client, "user1", "correct-horse-1", origin=origin)
    assert r.headers["location"] == "/login?error=origin"
    assert "set-cookie" not in r.headers


def test_rate_limit_blocks_even_correct_password(client, service, users):
    for _ in range(service.settings.login_max_failures):
        post_login(client, "user1", "wrong")
    r = post_login(client, "user1", "correct-horse-1")
    assert r.headers["location"].startswith("/login?error=limit")


def test_disabled_user_cannot_log_in(client, service, users):
    service.db.execute("UPDATE users SET disabled = true WHERE name = 'user1'")
    assert login(client, "user1") is None


def test_login_is_audited(client, service, users):
    login(client, "user1")
    post_login(client, "user2", "wrong")
    actions = [r["action"] for r in service.db.all("SELECT action FROM audit")]
    assert "login" in actions and "login.failed" in actions


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/desktop/user1/", "/desktop/user1/"),
        ("//evil.example/", "/"),
        ("https://evil.example/", "/"),
        ("/\\evil.example", "/"),
        ('/x" onmouseover="alert(1)', "/"),
        ("", "/"),
        (None, "/"),
    ],
)
def test_safe_next(target, expected):
    assert safe_next(target) == expected


def test_change_own_password(client, service, users):
    cookie = login(client, "user1")
    client.cookies.set("ow_session", cookie)
    r = client.post(
        "/account/password",
        data={"current": "correct-horse-1", "new": "brand-new-pass", "confirm": "brand-new-pass"},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/?pw=ok"
    new_cookie = r.cookies.get("ow_session")
    assert new_cookie and new_cookie != cookie
    # Old session signed out, new one works, Samba got the new password.
    assert service.sessions.resolve(cookie) is None
    assert service.sessions.resolve(new_cookie).name == "user1"
    assert service.samba.passwords["user1"] == "brand-new-pass"
    client.cookies.clear()
    assert login(client, "user1", "correct-horse-1") is None
    assert login(client, "user1", "brand-new-pass") is not None


def test_change_password_wrong_current(client, users):
    client.cookies.set("ow_session", login(client, "user1"))
    r = client.post(
        "/account/password",
        data={"current": "nope", "new": "brand-new-pass", "confirm": "brand-new-pass"},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/?pw=current"


def test_security_headers(client):
    r = client.get("/healthz")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
