# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Users without GPU access (#67)."""

import pytest

from orbitalwindow.desktops import GpuVendor, build_spec
from orbitalwindow.users import ValidationError

USER = {
    "name": "user1",
    "display_name": "User One",
    "uid_gid": 2001,
    "mac": "02:42:de:5c:00:01",
    "cpus": None,
    "mem_gb": None,
}


def gpu_access(service, name):
    """Return the stored GPU access of a user.

    Args:
        service: The service.
        name: User name.

    Returns:
        The ``gpu_access`` column.
    """
    return service.db.scalar("SELECT gpu_access FROM users WHERE name = ?", [name])


@pytest.mark.parametrize("vendor", list(GpuVendor))
def test_no_gpu_devices_without_gpu_access(settings, vendor):
    """Without GPU access a container gets no GPU devices, whatever the server's vendor."""
    spec = build_spec({**USER, "gpu_access": False}, "img", settings, vendor, "s3cret")
    assert spec.kwargs["devices"] == [] and spec.kwargs["device_requests"] == []
    assert "NVIDIA_DRIVER_CAPABILITIES" not in spec.kwargs["environment"]


def test_gpu_devices_with_gpu_access(settings):
    """With GPU access the container gets the server's GPU, as before."""
    spec = build_spec({**USER, "gpu_access": True}, "img", settings, GpuVendor.AMD, "s3cret")
    assert spec.kwargs["devices"] == ["/dev/dri:/dev/dri:rwm", "/dev/kfd:/dev/kfd:rwm"]


def test_gpu_access_is_the_default(users):
    """New users, admins too, have GPU access."""
    assert all(row["gpu_access"] for row in users.values())


def test_remove_gpu_access_removes_ai_manager(service, users):
    """Taking GPU access away also takes the AI manager role, and both are audited."""
    uid = users["user1"]["id"]
    service.set_ai_manager("boss", uid, True)
    service.set_gpu_access("boss", uid, False)
    row = service.db.one("SELECT gpu_access, is_ai_manager FROM users WHERE id = ?", [uid])
    assert row == {"gpu_access": False, "is_ai_manager": False}
    actions = service.db.all("SELECT action FROM audit WHERE target = 'user1' ORDER BY id")
    assert [a["action"] for a in actions][-3:] == [
        "user.ai_manager",
        "user.not_ai_manager",
        "user.no_gpu",
    ]
    service.set_gpu_access("boss", uid, True)
    assert gpu_access(service, "user1") is True


def test_no_ai_manager_without_gpu_access(service, users):
    """A user without GPU access cannot be made an AI manager."""
    uid = users["user1"]["id"]
    service.set_gpu_access("boss", uid, False)
    with pytest.raises(ValidationError, match="no GPU access"):
        service.set_ai_manager("boss", uid, True)
    service.set_ai_manager("boss", uid, False)


def test_admins_always_have_gpu_access(service, users):
    """An admin's GPU access can't be removed, and making a user admin gives it back."""
    with pytest.raises(ValidationError, match="Administrators"):
        service.set_gpu_access("system", users["boss"]["id"], False)
    uid = users["user2"]["id"]
    service.set_gpu_access("boss", uid, False)
    service.set_admin("boss", users["boss"]["id"], uid, True)
    assert gpu_access(service, "user2") is True


def test_recovery_gives_gpu_access(service, users):
    """The lock-out recovery makes the user an admin with GPU access."""
    service.set_gpu_access("boss", users["user1"]["id"], False)
    service.recover_admin("user1")
    assert gpu_access(service, "user1") is True
