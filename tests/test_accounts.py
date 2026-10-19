# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Account management: disable/enable, delete, admin rights, limits, settings, audit."""

from unittest.mock import MagicMock

import pytest

from orbitalwindow.users import ValidationError


@pytest.fixture
def stops(service, monkeypatch):
    """Record desktop stops and skip user image removal; return the stopped names."""
    stopped: list[str] = []
    monkeypatch.setattr(service.desktops, "stop", lambda user: stopped.append(user["name"]))
    monkeypatch.setattr(service.builder, "remove_user_image", lambda name: None)
    return stopped


def row(service, name):
    """Return a user's database row.

    Args:
        service: Service under test.
        name: User name.

    Returns:
        The user row, or None if there is no such user.
    """
    return service.db.one("SELECT * FROM users WHERE name = ?", [name])


def test_disable_blocks_everything_and_enable_restores(service, users, stops):
    """Disabling signs out, blocks login and stops the desktop; enabling restores."""
    boss, user1 = users["boss"], users["user1"]
    token = service.sessions.issue(user1["id"], user1["session_ver"])
    service.set_disabled("boss", boss["id"], user1["id"], True)
    assert row(service, "user1")["disabled"]
    assert service.sessions.resolve(token) is None  # signed out
    assert service.login("user1", "correct-horse-1", "1.2.3.4") is None
    assert stops == ["user1"]
    assert ("enabled", "user1", False) in service.samba.calls
    service.set_disabled("boss", boss["id"], user1["id"], False)
    assert not row(service, "user1")["disabled"]
    assert ("enabled", "user1", True) in service.samba.calls
    assert service.login("user1", "correct-horse-1", "1.2.3.4") is not None
    assert stops == ["user1"]  # enabling does not start the desktop


def test_admin_cannot_disable_or_delete_self(service, users, stops):
    """An admin cannot disable or delete their own account."""
    boss = users["boss"]
    with pytest.raises(ValidationError, match="your own account"):
        service.set_disabled("boss", boss["id"], boss["id"], True)
    with pytest.raises(ValidationError, match="your own account"):
        service.delete_user("boss", boss["id"], boss["id"])


def test_last_admin_is_protected(service, users, stops):
    """The last administrator cannot be disabled, deleted or demoted."""
    boss = users["boss"]
    # Acting as "system" (no actor id) still can't remove the last admin.
    for fn in (
        lambda: service.set_disabled("system", None, boss["id"], True),
        lambda: service.delete_user("system", None, boss["id"]),
        lambda: service.set_admin("system", None, boss["id"], False),
    ):
        with pytest.raises(ValidationError, match="last administrator"):
            fn()
    service.set_admin("boss", boss["id"], users["user1"]["id"], True)
    service.set_disabled("user1", users["user1"]["id"], boss["id"], True)  # now allowed


def test_delete_user(service, users, stops):
    """Deleting a user removes their rows and Samba account but keeps the home."""
    user2 = users["user2"]
    service.db.execute("INSERT INTO user_packages VALUES (?, 'octave')", [user2["id"]])
    service.grant_addon("boss", user2["id"], "matlab")
    result = service.delete_user("boss", users["boss"]["id"], user2["id"])
    assert row(service, "user2") is None
    assert stops == ["user2"]
    assert ("remove", "user2") in service.samba.calls
    for table in ("user_packages", "addon_grants", "user_addons", "user_image"):
        sql = f"SELECT count(*) FROM {table} WHERE user_id = ?"
        assert not service.db.scalar(sql, [user2["id"]])
    assert any("home folder was kept" in w for w in result.warnings)
    assert ("user2", 2002) in service.hostops.homes  # not removed
    assert service.db.one("SELECT * FROM audit WHERE action = 'user.delete'")["target"] == "user2"
    # UID/MAC are not reused by the next user.
    service.create_user(actor="boss", name="user5", display_name="5", password="long-enough-1")
    assert row(service, "user5")["uid_gid"] == 2004


def test_delete_user_with_home(service, users, stops):
    """Deleting a user with delete_home also removes the home folder."""
    service.delete_user("boss", users["boss"]["id"], users["user2"]["id"], delete_home=True)
    assert "user2" not in [h[0] for h in service.hostops.homes]


def test_limits(service, users):
    """CPU and memory limits are stored, cleared and validated."""
    uid = users["user1"]["id"]
    service.set_limits("boss", uid, "4", "16")
    assert (row(service, "user1")["cpus"], row(service, "user1")["mem_gb"]) == (4.0, 16.0)
    service.set_limits("boss", uid, "", None)
    assert (row(service, "user1")["cpus"], row(service, "user1")["mem_gb"]) == (None, None)
    for cpus, mem in (("lots", ""), ("0.1", ""), ("100000", ""), ("", "1"), ("-1", "")):
        with pytest.raises(ValidationError):
            service.set_limits("boss", uid, cpus, mem)


def test_create_user_validates_limits(service):
    """Creating a user with invalid limits is refused."""
    with pytest.raises(ValidationError):
        service.create_user(actor="t", name="bad", display_name="b", cpus="x", mem_gb="")


def test_settings(service):
    """Global settings are saved, normalised and validated."""
    service.update_settings(
        "boss",
        {
            "default_cpus": "2",
            "default_mem_gb": "",
            "default_quota_gb": "100",
            "scratch_retention_days": "14",
            "gpu_vendor": "amd",
            "hostname": "Desk.Example.org",
        },
    )
    s = service.get_settings()
    assert s["default_cpus"] == "2" and s["default_mem_gb"] == ""
    assert s["scratch_retention_days"] == "14" and s["gpu_vendor"] == "amd"
    assert service.public_hostname() == "desk.example.org"
    service.update_settings("boss", {"gpu_vendor": "intel"})
    assert service.get_settings()["gpu_vendor"] == "intel"
    for bad in (
        {"gpu_vendor": "matrox"},
        {"hostname": "bad host"},
        {"hostname": "x;rm"},
        {"scratch_retention_days": "0"},
        {"scratch_retention_days": "soon"},
        {"default_mem_gb": "1"},
    ):
        with pytest.raises(ValidationError):
            service.update_settings("boss", bad)


def test_audit_search(service, users):
    """Audit search filters by text and is safe against SQL injection."""
    rows = service.audit_log("user2")
    assert rows and all("user2" in (r["target"] + r["detail"] + r["actor"]) for r in rows)
    assert len(service.audit_log("")) >= 3
    assert service.audit_log("'; DROP TABLE audit; --") == []


def test_admin_forward_auth(client, users):
    """Admin forwardAuth redirects anonymous users and allows only admins."""
    from conftest import login

    headers = {
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "desk.test",
        "X-Forwarded-Uri": "/admin/cadvisor/",
    }
    client.cookies.clear()
    r = client.get("/auth/admin", headers=headers, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://desk.test/login")
    client.cookies.set("ow_session", login(client, "user1"))
    assert client.get("/auth/admin", headers=headers).status_code == 403
    client.cookies.set("ow_session", login(client, "boss"))
    r = client.get("/auth/admin", headers=headers)
    assert r.status_code == 200 and r.headers["remote-user"] == "boss"


def test_reconcile_removes_orphans(service, users):
    """Reconcile removes containers and networks of unknown or disabled users."""
    from orbitalwindow.reconcile import reconcile

    orphan = MagicMock(labels={"ow.user": "ghost", "ow.role": "desktop"}, name="ow-ghost")
    disabled = MagicMock(labels={"ow.user": "user2", "ow.role": "desktop"}, name="ow-user2")
    alive = MagicMock(labels={"ow.user": "user1", "ow.role": "desktop"}, name="ow-user1")
    service.db.execute("UPDATE users SET disabled = true WHERE name = 'user2'")

    def net(user):
        """Return a fake managed Docker network for a user.

        Args:
            user: User name for the ``ow.user`` label.

        Returns:
            A MagicMock network with managed labels and no containers.
        """
        return MagicMock(attrs={"Labels": {"ow.managed": "true", "ow.user": user}}, containers=[])

    ghost_net, live_net = net("ghost"), net("user1")
    service.client.containers.list.return_value = [orphan, disabled, alive]
    service.client.networks.list.return_value = [ghost_net, live_net]
    service.desktops.ensure_traefik_connected = lambda: 0
    reconcile(service)
    orphan.remove.assert_called_once_with(force=True)
    disabled.remove.assert_called_once_with(force=True)
    alive.remove.assert_not_called()
    ghost_net.remove.assert_called_once()
    live_net.remove.assert_not_called()
