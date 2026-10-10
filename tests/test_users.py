# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from orbitalwindow.users import ValidationError, mac_for, parse_limit, validate_name


@pytest.mark.parametrize("name", ["ab", "user1", "x" * 31])
def test_valid_names(name):
    assert validate_name(name) == name


@pytest.mark.parametrize(
    "name", ["a", "1user", "user-1", "user_1", "User!", "x" * 32, "root", "admin", "scratch"]
)
def test_invalid_names(name):
    with pytest.raises(ValidationError):
        validate_name(name)


def test_mac_format():
    assert mac_for(1) == "02:42:de:5c:00:01"
    assert mac_for(300) == "02:42:de:5c:01:2c"


def test_allocation_is_never_reused(service, users):
    uids = {u["uid_gid"] for u in users.values()}
    assert uids == {2001, 2002, 2003}
    service.db.execute("DELETE FROM users WHERE name = 'user2'")
    result = service.create_user(actor="t", name="user4", display_name="U4")
    user4 = service.db.one("SELECT * FROM users WHERE name = 'user4'")
    assert user4["uid_gid"] == 2004
    assert user4["mac"] == "02:42:de:5c:00:04"
    assert result.password  # generated and shown once


def test_create_user_side_effects(service):
    service.create_user(actor="boss", name="newbie", display_name="New", password="long-enough-1")
    assert ("newbie", 2001) in service.hostops.homes
    assert service.samba.passwords["newbie"] == "long-enough-1"
    assert ("sync",) in service.samba.calls
    audit = service.db.one("SELECT * FROM audit WHERE action = 'user.create'")
    assert audit["actor"] == "boss" and audit["target"] == "newbie"


def test_duplicate_user_is_refused(service, users):
    with pytest.raises(ValidationError):
        service.create_user(actor="t", name="user1", display_name="again")


def test_short_password_is_refused(service):
    with pytest.raises(ValidationError):
        service.create_user(actor="t", name="shorty", display_name="s", password="short")


@pytest.mark.parametrize(("value", "expected"), [("", None), (None, None), ("0", None), ("4", 4.0)])
def test_parse_limit(value, expected):
    assert parse_limit(value) == expected


def test_defaults_apply_unlimited(service):
    service.db.set_setting("default_cpus", "")
    service.db.set_setting("default_mem_gb", "16")
    service.create_user(actor="t", name="limited", display_name="L", password="long-enough-1")
    row = service.db.one("SELECT cpus, mem_gb FROM users WHERE name = 'limited'")
    assert row == {"cpus": None, "mem_gb": 16.0}
