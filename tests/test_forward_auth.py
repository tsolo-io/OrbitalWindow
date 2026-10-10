# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""forwardAuth matrix for /desktop/<user>/ (design: Verification → pytest)."""

import pytest
from conftest import login

FORWARDED = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "desk.test"}


def check(client, uri: str, cookie: str | None, headers: dict | None = None):
    client.cookies.clear()
    if cookie:
        client.cookies.set("ow_session", cookie)
    headers = (FORWARDED if headers is None else headers) | {"X-Forwarded-Uri": uri}
    return client.get("/auth/desktop", headers=headers, follow_redirects=False)


def test_no_cookie_redirects_to_login(client, users):
    r = check(client, "/desktop/user1/", None)
    assert r.status_code == 302
    # Absolute: Traefik would resolve a relative Location against the auth server.
    assert r.headers["location"] == "https://desk.test/login?next=/desktop/user1/"


@pytest.mark.parametrize("host", ["", "evil.example/x", "a b", "evil.example@x"])
def test_bad_forwarded_host_does_not_redirect(client, users, host):
    headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": host}
    assert check(client, "/desktop/user1/", None, headers).status_code == 401


def test_owner_is_allowed(client, users):
    r = check(client, "/desktop/user1/websockify?x=1", login(client, "user1"))
    assert r.status_code == 200
    assert r.headers["x-ow-user"] == "user1"


def test_other_user_is_forbidden(client, users):
    assert check(client, "/desktop/user2/", login(client, "user1")).status_code == 403


def test_admin_is_forbidden_on_user_desktop(client, users):
    assert check(client, "/desktop/user1/", login(client, "boss")).status_code == 403


def test_admin_may_open_own_desktop(client, users):
    assert check(client, "/desktop/boss/", login(client, "boss")).status_code == 200


@pytest.mark.parametrize("uri", ["/desktop/user1x/", "/desktop/user1-x/", "/desktop/", "/", ""])
def test_prefix_tricks_are_forbidden(client, users, uri):
    assert check(client, uri, login(client, "user1")).status_code == 403


def test_bare_path_without_slash_is_owner_only(client, users):
    cookie = login(client, "user1")
    assert check(client, "/desktop/user1", cookie).status_code == 200


def test_tampered_cookie_redirects(client, users):
    cookie = login(client, "user1")
    assert check(client, "/desktop/user1/", cookie[:-2] + "xx").status_code == 302


def test_password_change_signs_out_old_sessions(client, service, users):
    cookie = login(client, "user1")
    service.admin_set_password("boss", users["user1"]["id"], "another-pass-2")
    assert check(client, "/desktop/user1/", cookie).status_code == 302


def test_disabled_user_is_signed_out(client, service, users):
    cookie = login(client, "user1")
    service.db.execute("UPDATE users SET disabled = true WHERE name = 'user1'")
    assert check(client, "/desktop/user1/", cookie).status_code == 302


def test_session_paths_are_owner_only(client, users):
    cookie = login(client, "user1")
    assert check(client, "/desktop/user1/a1b2c3d4/api/websockets", cookie).status_code == 200
    assert check(client, "/desktop/user2/a1b2c3d4/", cookie).status_code == 403
