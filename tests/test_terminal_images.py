# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Terminal-only images (no desktop): adding, building, the container spec and add-ons."""

import pytest

from orbitalwindow.desktops import DesktopState, GpuVendor, build_spec
from orbitalwindow.service import _image_reference, _terminal_name
from orbitalwindow.users import ValidationError


@pytest.mark.parametrize(
    ("text", "ref", "name"),
    [
        (
            "baseimage-ubuntu:noble",
            "lscr.io/linuxserver/baseimage-ubuntu:noble",
            "baseimage-ubuntu-noble",
        ),
        (
            " lscr.io/linuxserver/baseimage-fedora:42 ",
            "lscr.io/linuxserver/baseimage-fedora:42",
            "baseimage-fedora-42",
        ),
        (
            "baseimage-debian",
            "lscr.io/linuxserver/baseimage-debian:latest",
            "baseimage-debian-latest",
        ),
        ("ghcr.io/org/tools:1.2", "ghcr.io/org/tools:1.2", "tools-1.2"),
    ],
)
def test_image_reference_and_name(text, ref, name):
    """Short LinuxServer names get the registry and a tag; the name is the image and tag."""
    assert _image_reference(text) == ref
    assert _terminal_name(ref) == name


@pytest.mark.parametrize("text", ["", "UPPER:case", "a b", "x//y", "../x", "a:b:c"])
def test_invalid_image_references(text):
    """Things that aren't image references are refused."""
    with pytest.raises(ValidationError, match="not a valid image"):
        _image_reference(text)


def test_add_terminal_image(service):
    """A terminal-only image is stored without a desktop and queued for a build."""
    image_id = service.add_terminal_image("boss", "baseimage-ubuntu:noble")
    row = service.db.one("SELECT * FROM images WHERE id = ?", [image_id])
    assert row["gui"] is False and row["label"] == "Ubuntu Noble"
    assert row["image_ref"] == "ow-terminal:baseimage-ubuntu-noble"
    assert row["base_image"] == "lscr.io/linuxserver/baseimage-ubuntu:noble"
    assert service.builder.latest_build("base", image_id)["status"] == "queued"
    with pytest.raises(ValidationError, match="already exists"):
        service.add_terminal_image("boss", "lscr.io/linuxserver/baseimage-ubuntu:noble")


def test_terminal_image_builds_from_its_own_dockerfile(service):
    """The build uses Dockerfile.terminal on the stored base image; desktops keep Dockerfile."""
    image_id = service.add_terminal_image("boss", "baseimage-ubuntu:noble")
    build_id = service.builder.latest_build("base", image_id)["id"]
    service.client.containers.run.return_value = b"bash\ntmux\n"
    service.builder._build_base(build_id, image_id)
    kwargs = service.client.api.build.call_args.kwargs
    assert kwargs["dockerfile"] == "Dockerfile.terminal"
    assert kwargs["buildargs"] == {
        "BASE_IMAGE": "lscr.io/linuxserver/baseimage-ubuntu:noble",
        "GPU_VENDOR": "nvidia",
    }
    assert kwargs["tag"] == "ow-terminal:baseimage-ubuntu-noble"
    service.builder._build_base(service.builder.queue_base(1), 1)
    kwargs = service.client.api.build.call_args.kwargs
    assert kwargs["dockerfile"] == "Dockerfile"
    assert kwargs["buildargs"]["BASE_IMAGE"].endswith("webtop:ubuntu-xfce")


def test_terminal_spec_has_no_web_route_or_desktop_settings(settings):
    """Without a desktop: no Traefik labels, no desktop password or path; OW_GUI=false."""
    user = {"name": "user1", "display_name": "U", "uid_gid": 2001, "mac": "02:42:de:5c:00:01"}
    spec = build_spec(user, "ow-terminal:x", settings, GpuVendor.NVIDIA, "s3cret", gui=False)
    labels, env = spec.kwargs["labels"], spec.kwargs["environment"]
    assert not any(key.startswith("traefik.") for key in labels)
    assert labels["ow.role"] == "desktop" and labels["ow.user"] == "user1"
    assert env["OW_GUI"] == "false" and env["PUID"] == "2001"
    assert not {"PASSWORD", "SUBFOLDER", "CUSTOM_USER", "TITLE"} & set(env)
    # Same resources as a desktop.
    assert spec.kwargs["shm_size"] == "2g" and spec.kwargs["device_requests"]


def choose_terminal(service, user_id):
    """Add a ready terminal-only image and switch the user to it.

    Args:
        service: Service under test.
        user_id: Id of the user to switch.

    Returns:
        The id of the terminal-only image.
    """
    image_id = service.db.scalar(
        "INSERT INTO images (label, base_tag, image_ref, status, built_at, docker_id, gui) "
        "VALUES ('Ubuntu', 'baseimage-ubuntu-noble', 'ow-terminal:baseimage-ubuntu-noble', "
        "'ready', now(), 'sha256:t', false) RETURNING id"
    )
    service.desktops.state = lambda name: DesktopState.STOPPED
    service.choose_image("user1", user_id, image_id)
    return image_id


def test_choosing_a_terminal_image_switches_on_ssh(service, users):
    """SSH is switched on automatically, and a build is queued for it."""
    uid = users["user1"]["id"]
    choose_terminal(service, uid)
    assert "ssh" in {a.key for a in service.software.active_addons(uid)}
    assert service.db.scalar("SELECT status FROM user_image WHERE user_id = ?", [uid]) == "queued"


def test_ssh_stays_off_when_not_allowed(service, users):
    """A restricted SSH add-on without a grant is left off; choosing still works."""
    service.set_addon_restricted("boss", "ssh", True)
    uid = users["user1"]["id"]
    image_id = choose_terminal(service, uid)
    assert service.db.scalar("SELECT image_id FROM users WHERE id = ?", [uid]) == image_id
    assert "ssh" not in {a.key for a in service.software.active_addons(uid)}


def test_desktop_addons_need_a_desktop(service, users):
    """GUI add-ons can't be used without a desktop, and drop out of the image plan."""
    uid = users["user1"]["id"]
    service.set_addon("user1", uid, "vscode", True)
    service.set_addon("user1", uid, "devtools", True)
    choose_terminal(service, uid)
    states = {s.addon.key: s for s in service.software.addon_states(uid)}
    assert states["vscode"].needs_desktop and not states["vscode"].active
    assert not states["devtools"].needs_desktop and states["devtools"].active
    with pytest.raises(ValidationError, match="needs a desktop"):
        service.set_addon("user1", uid, "vscode", True)
    plan = service.software.plan(service.db.one("SELECT * FROM users WHERE id = ?", [uid]))
    assert "vscode" not in {a.key for a in plan.addons}


def test_terminal_start_passes_no_gui(service, users, monkeypatch):
    """Starting a terminal-only image starts a container without a desktop."""
    uid = users["user1"]["id"]
    image_id = choose_terminal(service, uid)
    service.db.execute("DELETE FROM user_addons")  # no per-user image needed
    started = {}
    monkeypatch.setattr(
        service.desktops, "start", lambda user, ref, *args, gui: started.update(ref=ref, gui=gui)
    )
    service.start_desktop("user1", uid)
    assert started == {"ref": "ow-terminal:baseimage-ubuntu-noble", "gui": False}
    assert image_id
