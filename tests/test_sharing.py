# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Groups, file managers, forwardAuth for /files, disk usage, scratch clean-up, scheduling."""

from datetime import datetime, timedelta

import pytest
import yaml

from orbitalwindow import reconcile as reconcile_mod
from orbitalwindow.desktops import GpuVendor, build_spec
from orbitalwindow.files import FB_ADMIN, build_files_spec, files_config
from orbitalwindow.users import ValidationError


def user(service, name):
    return service.db.one("SELECT * FROM users WHERE name = ?", [name])


@pytest.fixture
def labx(service, users):
    service.create_group("boss", "labx", "Lab X", "100")
    gid = service.db.scalar("SELECT id FROM groups WHERE name = 'labx'")
    service.set_group_members("boss", gid, [users["user1"]["id"], users["user2"]["id"]])
    return service.db.one("SELECT * FROM groups WHERE id = ?", [gid])


def test_group_names_share_the_user_namespace(service, users):
    with pytest.raises(ValidationError, match="already a user"):
        service.create_group("boss", "user1")
    with pytest.raises(ValidationError, match="reserved"):
        service.create_group("boss", "scratch")
    service.create_group("boss", "proj")
    with pytest.raises(ValidationError, match="already a group"):
        service.create_user(actor="boss", name="proj", display_name="P", password="long-enough-1")


def test_create_group_and_members(service, users, labx):
    assert labx["gid"] == 3001 and labx["quota_gb"] == 100
    assert service.hostops.group_dirs == {"labx": 3001}
    assert [g["name"] for g in service.groups_of(users["user1"]["id"])] == ["labx"]
    assert service.groups_of(users["boss"]["id"]) == []
    # Members' file managers were recreated with the group; Samba was synced.
    assert {"user1", "user2"} <= set(service.files.recreated)
    assert service.files.running["user1"] == ["labx"]
    assert ("sync",) in service.samba.calls


def test_samba_users_json_has_groups(service, users, labx, tmp_path):
    from orbitalwindow.samba import Samba

    samba = Samba(service.client, "ow-samba", tmp_path)
    samba.write_users(service.db)
    data = yaml.safe_load((tmp_path / "users.json").read_text())
    assert data["groups"] == [{"name": "labx", "gid": 3001, "members": ["user1", "user2"]}]


def test_remove_member_and_delete_group(service, users, labx):
    service.set_group_members("boss", labx["id"], [users["user1"]["id"]])
    assert service.groups_of(users["user2"]["id"]) == []
    assert service.files.running["user2"] == []
    result = service.delete_group("boss", labx["id"])
    assert any("kept" in w for w in result.warnings)
    assert service.hostops.removed_group_dirs == []
    assert service.groups_of(users["user1"]["id"]) == []
    # GIDs are never reused.
    service.create_group("boss", "laby")
    assert service.db.scalar("SELECT gid FROM groups WHERE name = 'laby'") == 3002


def test_delete_group_with_folder(service, users, labx):
    service.delete_group("boss", labx["id"], delete_folder=True)
    assert service.hostops.removed_group_dirs == ["labx"]


def test_unknown_member_is_refused(service, users, labx):
    with pytest.raises(ValidationError):
        service.set_group_members("boss", labx["id"], [9999])


def test_desktop_spec_has_group_mounts(service, settings, users, labx):
    groups = service.groups_of(users["user1"]["id"])
    spec = build_spec(user(service, "user1"), "img", settings, GpuVendor.NONE, "s", (), groups)
    k = spec.kwargs
    mount = k["volumes"][str(settings.root / "groups/labx")]
    assert mount == {"bind": "/groups/labx", "mode": "rw"}
    assert k["environment"]["OW_GROUPS"] == "labx:3001"
    assert k["labels"]["ow.groups"] == "labx"


def test_group_restart_notice(service, users, labx, monkeypatch):
    uid = users["user1"]["id"]
    monkeypatch.setattr(service.desktops, "groups_in_use", lambda name: set())
    assert service.group_restart_needed(uid)
    monkeypatch.setattr(service.desktops, "groups_in_use", lambda name: {"labx"})
    assert not service.group_restart_needed(uid)
    monkeypatch.setattr(service.desktops, "groups_in_use", lambda name: None)  # not running
    assert not service.group_restart_needed(uid)


def test_files_spec_and_config(settings, users, service, labx):
    groups = service.groups_of(users["user1"]["id"])
    spec = build_files_spec(user(service, "user1"), groups, settings)
    k = spec.kwargs
    assert spec.name == "ow-files-user1" and spec.network == "ow-fnet-user1"
    assert k["user"] == "2001:2001" and k["group_add"] == ["3001"]
    assert k["cap_drop"] == ["ALL"] and k["read_only"]
    assert k["healthcheck"]["test"][-1] == "http://localhost:8080/files/user1/health"
    assert k["volumes"][str(settings.root / "users/user1")]["bind"] == "/srv/home"
    assert k["volumes"][str(settings.root / "shared/data")]["mode"] == "ro"
    labels = k["labels"]
    assert labels["traefik.docker.network"] == "ow-fnet-user1"
    assert labels["traefik.http.routers.ow-files-user1.middlewares"] == (
        "ow-files-slash@docker,ow-auth-files@docker"
    )
    # A different group list changes the spec hash (container gets recreated).
    assert build_files_spec(user(service, "user1"), [], settings).spec_hash != spec.spec_hash

    config = files_config("user1", ["labx"], "pw")
    assert config["http"]["baseURL"] == "/files/user1/"
    assert config["server"]["filesystem"] == {
        "createFilePermission": "664",
        "createDirectoryPermission": "2775",
    }
    assert config["auth"]["methods"]["proxy"] == {"enabled": True, "header": "X-OW-User"}
    assert config["auth"]["methods"]["password"] == {"enabled": False}
    assert config["auth"]["adminUsername"] == FB_ADMIN  # never a valid OrbitalWindow name
    assert config["userDefaults"]["account"]["permissions"]["share"] is False
    sources = {s["name"]: s for s in config["server"]["sources"]}
    assert set(sources) == {"Home", "Group labx", "Scratch", "Shared data"}
    assert all(s["config"]["private"] for s in sources.values())
    assert sources["Shared data"]["config"]["readOnly"]
    assert not sources["Shared data"]["config"]["defaultPermissions"]["modify"]


def test_files_forward_auth(client, users):
    from conftest import login

    def check(cookie, uri):
        client.cookies.clear()
        if cookie:
            client.cookies.set("ow_session", cookie)
        headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "desk.test",
                   "X-Forwarded-Uri": uri}  # fmt: skip
        return client.get("/auth/files", headers=headers, follow_redirects=False)

    user1, boss = login(client, "user1"), login(client, "boss")
    assert check(None, "/files/user1/").status_code == 302
    r = check(user1, "/files/user1/api/resources?source=Home")
    assert r.status_code == 200 and r.headers["x-ow-user"] == "user1"
    assert check(user1, "/files/user2/").status_code == 403
    assert check(boss, "/files/user1/").status_code == 403
    assert check(user1, "/desktop/user1/").status_code == 403  # wrong kind


def test_lifecycle_of_file_manager(service, users):
    boss_id, uid = users["boss"]["id"], users["user2"]["id"]
    assert "user2" in service.files.running  # created with the user
    service.set_disabled("boss", boss_id, uid, True)
    assert "user2" not in service.files.running
    service.set_disabled("boss", boss_id, uid, False)
    assert "user2" in service.files.running
    service.delete_user("boss", boss_id, uid)
    assert "user2" not in service.files.running and "user2" not in service.hostops.files_data


def test_disk_usage_and_quota(service, users, labx):
    gib = 1024**3
    service.set_quota("boss", users["user1"]["id"], "50")
    service.hostops.sizes = {
        "users/user1": 46 * gib,  # quota 50 → 92% → warning
        "users/user2": 1 * gib,
        "groups/labx": 10 * gib,
        "shared/scratch": 5,
    }
    assert service.measure_disk() == 4
    u1 = service.disk_usage_for(users["user1"]["id"])
    assert u1.quota_gb == 50 and u1.warning
    assert not service.disk_usage_for(users["user2"]["id"]).warning
    usage, host = service.disk_overview()
    assert [u.name for u in usage][:2] == ["user1", "labx"]  # largest first
    assert next(u for u in usage if u.name == "labx").quota_gb == 100
    assert host.total > 0


def test_scheduled_jobs(service, users, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "measure_disk", lambda: calls.append("disk"))
    monkeypatch.setattr(service, "clean_scratch", lambda: calls.append("scratch"))
    night = datetime(2026, 10, 9, 1, 0)
    reconcile_mod.run_scheduled(service, night)
    assert calls == ["disk"]  # before 02:00: no clean-up yet
    reconcile_mod.run_scheduled(service, night + timedelta(minutes=30))
    assert calls == ["disk"]  # disk is hourly
    reconcile_mod.run_scheduled(service, night + timedelta(hours=1, minutes=5))
    assert calls == ["disk", "disk", "scratch"]
    reconcile_mod.run_scheduled(service, night + timedelta(hours=5))
    assert calls.count("scratch") == 1  # once a day
    reconcile_mod.run_scheduled(service, night + timedelta(days=1, hours=1))
    assert calls.count("scratch") == 2


def test_clean_scratch_is_audited(service):
    assert service.clean_scratch() == 1
    assert service.db.one("SELECT * FROM audit WHERE action = 'scratch.clean'")


def test_retention_change_rewrites_readme(service):
    service.update_settings("boss", {"scratch_retention_days": "14"})
    assert service.hostops.readme_days == [14]
