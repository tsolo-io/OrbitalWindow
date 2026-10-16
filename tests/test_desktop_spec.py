# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Container spec snapshot (design: Desktop container spec)."""

import base64

from orbitalwindow.desktops import GpuVendor, build_spec

USER = {
    "name": "user1",
    "display_name": "User One",
    "uid_gid": 2001,
    "mac": "02:42:de:5c:00:01",
    "cpus": 4.0,
    "mem_gb": 16.0,
}


def test_spec_snapshot(settings):
    """The desktop container spec matches the expected snapshot."""
    spec = build_spec(
        USER, "ow-desktop:ubuntu-xfce", settings, GpuVendor.NVIDIA, "s3cret", session="a1b2c3d4"
    )
    root = str(settings.root)
    assert spec.name == "ow-user1"
    assert spec.network == "ow-net-user1"
    assert spec.mac == "02:42:de:5c:00:01"
    k = spec.kwargs
    assert k["hostname"] == "ow-user1"
    assert k["cap_drop"] == ["NET_RAW"]
    assert k["pids_limit"] == 4096
    assert k["shm_size"] == "2g"
    assert k["nano_cpus"] == 4_000_000_000
    assert k["mem_limit"] == 16 * 1024**3
    assert k["restart_policy"] == {"Name": "unless-stopped"}
    assert k["devices"] == ["/dev/dri:/dev/dri:rwm"]
    assert len(k["device_requests"]) == 1 and k["device_requests"][0]["Count"] == -1
    assert k["environment"] == {
        "PUID": "2001",
        "PGID": "2001",
        "TZ": "Etc/UTC",
        "TITLE": "User One – OrbitalWindow",
        "CUSTOM_USER": "user1",
        "PASSWORD": "s3cret",
        "SUBFOLDER": "/desktop/user1/a1b2c3d4/",
        "PIXELFLUX_WAYLAND": "false",
        "START_DOCKER": "false",
        "NVIDIA_DRIVER_CAPABILITIES": "all",
        "OW_GROUPS": "",
        "OW_USER_ENV": "",
    }
    assert k["volumes"] == {
        f"{root}/users/user1": {"bind": "/config", "mode": "rw"},
        f"{root}/shared/scratch": {"bind": "/scratch", "mode": "rw"},
        f"{root}/shared/data": {"bind": "/shared", "mode": "ro"},
    }
    labels = k["labels"]
    assert labels["ow.managed"] == "true" and labels["ow.user"] == "user1"
    assert labels["ow.session"] == "a1b2c3d4"
    assert labels["traefik.docker.network"] == "ow-net-user1"
    assert (
        labels["traefik.http.routers.ow-user1.rule"]
        == "Path(`/desktop/user1/a1b2c3d4`) || PathPrefix(`/desktop/user1/a1b2c3d4/`)"
    )
    assert labels["traefik.http.routers.ow-user1.middlewares"] == (
        "ow-desktop-slash@docker,ow-auth-desktop@docker,ow-user1-basic"
    )
    header = labels[
        "traefik.http.middlewares.ow-user1-basic.headers.customrequestheaders.Authorization"
    ]
    assert base64.b64decode(header.removeprefix("Basic ")) == b"user1:s3cret"
    assert labels["traefik.http.services.ow-user1.loadbalancer.server.port"] == "3000"


def test_unlimited_and_no_gpu(settings):
    """No limits and no GPU leave out the limit and device options."""
    user = USER | {"cpus": None, "mem_gb": None}
    k = build_spec(user, "img", settings, GpuVendor.NONE, "x").kwargs
    assert "nano_cpus" not in k and "mem_limit" not in k
    assert k["devices"] == [] and k["device_requests"] == []
    assert "NVIDIA_DRIVER_CAPABILITIES" not in k["environment"]


def test_amd(settings):
    """An AMD GPU passes /dev/dri and /dev/kfd without device requests."""
    k = build_spec(USER, "img", settings, GpuVendor.AMD, "x").kwargs
    assert k["devices"] == ["/dev/dri:/dev/dri:rwm", "/dev/kfd:/dev/kfd:rwm"]
    assert k["device_requests"] == []


def test_state_mapping():
    """Docker container states map to the right desktop states."""
    from orbitalwindow.desktops import DesktopState, _state_of

    assert _state_of({"Status": "running"}) is DesktopState.RUNNING
    assert _state_of({"Status": "running", "Health": {"Status": "starting"}}) is (
        DesktopState.STARTING
    )
    assert _state_of({"Status": "running", "Health": {"Status": "healthy"}}) is (
        DesktopState.RUNNING
    )
    assert _state_of({"Status": "created"}) is DesktopState.STARTING
    assert _state_of({"Status": "exited"}) is DesktopState.ERROR


def test_each_start_gets_its_own_path(settings):
    """Every desktop start gets its own URL path."""
    from orbitalwindow.desktops import desktop_path

    assert desktop_path("user1") == "/desktop/user1/"
    assert desktop_path("user1", "a1b2c3d4") == "/desktop/user1/a1b2c3d4/"
    first = build_spec(USER, "img", settings, GpuVendor.NONE, "x").kwargs["environment"]
    second = build_spec(USER, "img", settings, GpuVendor.NONE, "x").kwargs["environment"]
    assert first["SUBFOLDER"] != second["SUBFOLDER"]
    assert first["SUBFOLDER"].startswith("/desktop/user1/")
