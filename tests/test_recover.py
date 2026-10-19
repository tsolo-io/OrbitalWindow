# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Lock-out recovery: make reset-admin-password (#42)."""

import pytest

from orbitalwindow import recover


def test_disabled_user_becomes_an_enabled_admin(service, users):
    """A disabled non-admin gets a new password, is enabled and made admin; it is audited."""
    user1 = users["user1"]
    service.set_disabled("boss", users["boss"]["id"], user1["id"], True)
    result = service.recover_admin("user1")
    row = service.db.one("SELECT * FROM users WHERE name = 'user1'")
    assert not row["disabled"] and row["is_admin"]
    assert row["session_ver"] > user1["session_ver"]  # other sessions are signed out
    assert service.login("user1", result.password, "10.0.0.1") is not None
    assert service.samba.passwords["user1"] == result.password
    assert ("enabled", "user1", True) in service.samba.calls
    audit = service.db.one("SELECT * FROM audit WHERE action = 'user.recover'")
    assert audit["actor"] == "system" and audit["detail"] == "enabled, made admin"


def test_a_samba_failure_does_not_stop_the_reset(service, users):
    """When Samba can't be reached the password is still reset, with a warning."""

    def down(name, password):
        """Fail like a stopped socket proxy.

        Args:
            name: User name.
            password: The new password.

        Raises:
            ConnectionError: Always.
        """
        raise ConnectionError("socket-proxy-rw: connection refused")

    service.samba.set_password = down
    result = service.recover_admin("boss")
    assert service.login("boss", result.password, "10.0.0.1") is not None
    assert "Samba" in result.warnings[0] and "connection refused" in result.warnings[0]


def test_a_deleted_admin_is_created_again(service):
    """With no such user, an administrator of that name is created."""
    result = service.recover_admin("admin")
    row = service.db.one("SELECT * FROM users WHERE name = 'admin'")
    assert row["is_admin"] and service.login("admin", result.password, "10.0.0.1") is not None


def test_command_prints_the_password_once(service, users, monkeypatch, capsys):
    """The command resets the named user (default admin) and prints the new password."""
    monkeypatch.setattr(recover, "Service", lambda settings, client, prepare_layout: service)
    monkeypatch.setattr(recover.docker, "from_env", lambda: None)
    monkeypatch.setattr(service.db, "close", lambda: None)
    assert recover.main(["user1"]) == 0
    out = capsys.readouterr().out
    password = out.splitlines()[-1].strip()
    assert "user1 is an enabled administrator again" in out
    assert service.login("user1", password, "10.0.0.1") is not None
    assert recover.main(["newadmin"]) == 0
    assert "newadmin was created as an administrator" in capsys.readouterr().out
    assert recover.main(["a", "b"]) == 1
    assert recover.main(["Bad Name!"]) == 1 and "not reset" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["--help"], ["-x"]])
def test_command_usage(args, capsys):
    """Options are not accepted; the usage is printed."""
    assert recover.main(args) == 1 and "Usage" in capsys.readouterr().err
