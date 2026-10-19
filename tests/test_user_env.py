# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Users' own environment variables: rules, storage, the desktop spec and restarts."""

import subprocess
from pathlib import Path

import pytest

from orbitalwindow import userenv
from orbitalwindow.addons import Addon, AddonSpec
from orbitalwindow.desktops import GpuVendor, build_spec
from orbitalwindow.users import ValidationError

INIT_HOOK = Path(__file__).parent.parent / "deploy/image/root/custom-cont-init.d/50-ow-setup"
USER = {
    "name": "user1",
    "display_name": "User One",
    "uid_gid": 2001,
    "mac": "02:42:de:5c:00:01",
    "cpus": None,
    "mem_gb": None,
}


@pytest.mark.parametrize("name", ["HF_HOME", "http_proxy", "_X", "A1"])
def test_valid_names(name):
    """Shell variable names, upper or lower case, are allowed."""
    assert userenv.validate_name(f" {name} ") == name


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("1ABC", "letters"),
        ("MY-VAR", "letters"),
        ("", "letters"),
        ("X" * 65, "letters"),
        ("PATH", "set by OrbitalWindow"),
        ("password", "set by OrbitalWindow"),
        ("PUID", "set by OrbitalWindow"),
        ("OW_ANYTHING", "set by OrbitalWindow"),
        ("NVIDIA_VISIBLE_DEVICES", "set by OrbitalWindow"),
    ],
)
def test_invalid_names(name, message):
    """Bad names and names OrbitalWindow or the start-up relies on are refused."""
    with pytest.raises(ValidationError, match=message):
        userenv.validate_name(name)


def test_values():
    """Values are kept as entered; line breaks and very long values are refused."""
    assert userenv.validate_value(" a b=c ") == " a b=c "
    for bad in ("a\nb", "x" * (userenv.MAX_VALUE + 1)):
        with pytest.raises(ValidationError):
            userenv.validate_value(bad)


def test_hash_ignores_order():
    """The label hash depends on names and values, not their order."""
    assert userenv.env_hash({"A": "1", "B": "2"}) == userenv.env_hash({"B": "2", "A": "1"})
    assert userenv.env_hash({"A": "1"}) != userenv.env_hash({"A": "2"})


def test_spec_sets_user_env_over_addons(settings):
    """The user's variables reach the container, win over an add-on's, and are labelled."""
    addon_spec = AddonSpec(key="hf", label="HF", env={"HF_HOME": "/opt/hf", "B": "addon"})
    addon = Addon(addon_spec, Path("."), "h")
    spec = build_spec(
        USER,
        "img",
        settings,
        GpuVendor.NONE,
        "s",
        [addon],
        user_env={"HF_HOME": "/config/hf", "A": "1", "PUID": "0"},
    )
    env = spec.kwargs["environment"]
    assert env["HF_HOME"] == "/config/hf" and env["A"] == "1" and env["B"] == "addon"
    assert env["PUID"] == "2001"  # reserved names never get through
    assert env["OW_USER_ENV"] == "A HF_HOME"
    expected = userenv.env_hash({"HF_HOME": "/config/hf", "A": "1"})
    assert spec.kwargs["labels"]["ow.env"] == expected


def test_store_rename_and_remove(service, users):
    """Variables are stored per user; renaming keeps one row; values stay out of the audit."""
    uid = users["user1"]["id"]
    service.set_user_env("user1", uid, "TOKEN", "sk-secret")
    service.set_user_env("user1", uid, "HF_HOME", "/config/hf")
    with pytest.raises(ValidationError, match="already have"):
        service.set_user_env("user1", uid, "TOKEN", "x")
    service.set_user_env("user1", uid, "API_TOKEN", "sk-new", old_name="TOKEN")
    assert service.user_env_values(uid) == {"API_TOKEN": "sk-new", "HF_HOME": "/config/hf"}
    assert service.user_env_values(users["user2"]["id"]) == {}
    service.remove_user_env("user1", uid, "HF_HOME")
    assert list(service.user_env_values(uid)) == ["API_TOKEN"]
    assert service.db.scalar("SELECT count(*) FROM audit WHERE detail LIKE '%sk-%'") == 0


def test_limit_on_number_of_variables(service, users, monkeypatch):
    """A user can have at most MAX_VARS variables."""
    monkeypatch.setattr(userenv, "MAX_VARS", 2)
    uid = users["user1"]["id"]
    service.set_user_env("user1", uid, "A", "1")
    service.set_user_env("user1", uid, "B", "2")
    service.set_user_env("user1", uid, "B", "3", old_name="B")  # changing one is fine
    with pytest.raises(ValidationError, match="at most 2"):
        service.set_user_env("user1", uid, "C", "3")


def test_restart_needed_compares_the_label(service, users):
    """A restart is needed when the running desktop's label differs from the saved variables."""
    uid = users["user1"]["id"]
    container = service.client.containers.get.return_value
    container.labels = {"ow.env": userenv.env_hash({})}
    assert not service.env_restart_needed(uid)
    service.set_user_env("user1", uid, "A", "1")
    assert service.env_restart_needed(uid)
    container.labels = {"ow.env": userenv.env_hash({"A": "1"})}
    assert not service.env_restart_needed(uid)


def test_deleting_a_user_removes_their_variables(service, users):
    """Deleting a user removes their variables."""
    uid = users["user1"]["id"]
    service.set_user_env("user1", uid, "A", "1")
    service.delete_user("boss", users["boss"]["id"], uid)
    assert service.db.scalar("SELECT count(*) FROM user_env") == 0


def test_init_hook_writes_a_profile_script_for_ssh(tmp_path):
    """The init hook's profile snippet exports the listed variables, quoted, for login shells."""
    hook = INIT_HOOK.read_text()
    start = hook.index("# User environment variables")
    end = hook.index("# END user environment variables")
    snippet = hook[start:end].replace("/etc/profile.d/ow-user-env.sh", str(tmp_path / "env.sh"))
    env = {"PATH": "/usr/bin:/bin", "OW_USER_ENV": "A B", "A": "it's $HOME", "B": "x y"}
    subprocess.run(["bash", "-c", snippet], env=env, check=True)
    out = subprocess.run(
        ["bash", "-c", f'. {tmp_path / "env.sh"}; printf "%s|%s" "$A" "$B"'],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout == "it's $HOME|x y"
