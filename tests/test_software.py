# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Add-on switches, grants, packages, per-user image planning and which image a start uses."""

import pytest

from orbitalwindow.builds import user_dockerfile
from orbitalwindow.desktops import DesktopError, DesktopState, GpuVendor, build_spec
from orbitalwindow.users import ValidationError


@pytest.fixture
def user1(service, users):
    """Fill the package index for base image 1 and return user1."""
    service.db.execute(
        "INSERT INTO pkg_index SELECT 1, unnest(['octave', 'gimp', 'octave-doc', 'r-base'])"
    )
    return users["user1"]


def get(service, user_id):
    """Return a user's database row by id.

    Args:
        service: Service under test.
        user_id: User id.

    Returns:
        The user row, or None if there is no such user.
    """
    return service.db.one("SELECT * FROM users WHERE id = ?", [user_id])


def test_restricted_addon_needs_grant(service, user1):
    """A restricted add-on can only be switched on after a grant."""
    states = {s.addon.key: s for s in service.software.addon_states(user1["id"])}
    assert not states["matlab"].available and states["vscode"].available
    with pytest.raises(ValidationError, match="Ask your administrator"):
        service.set_addon("user1", user1["id"], "matlab", True)
    service.grant_addon("boss", user1["id"], "matlab")
    service.set_addon("user1", user1["id"], "matlab", True)
    assert [a.key for a in service.software.active_addons(user1["id"])] == ["matlab"]


def test_disabled_addon_is_not_offered(service, user1):
    """An add-on disabled by the admin is neither offered nor active."""
    service.set_addon("user1", user1["id"], "vscode", True)
    service.set_addon_enabled("boss", "vscode", False)
    assert "vscode" not in {s.addon.key for s in service.software.addon_states(user1["id"])}
    assert service.software.active_addons(user1["id"]) == []


def test_restricted_override(service, user1):
    """The admin can override whether an add-on is restricted."""
    service.set_addon_restricted("boss", "matlab", False)
    service.set_addon("user1", user1["id"], "matlab", True)
    service.set_addon_restricted("boss", "vscode", True)
    with pytest.raises(ValidationError):
        service.set_addon("user1", user1["id"], "vscode", True)


def test_packages_are_checked_against_the_index(service, user1):
    """Packages are validated against the package index."""
    assert service.add_package("user1", user1["id"], " Octave ") == "octave"
    with pytest.raises(ValidationError, match="no package called"):
        service.add_package("user1", user1["id"], "does-not-exist")
    with pytest.raises(ValidationError, match="valid package name"):
        service.add_package("user1", user1["id"], "octave; rm -rf /")
    assert service.software.packages(user1["id"]) == ["octave"]
    assert service.software.search_packages(1, "oct") == ["octave", "octave-doc"]
    assert service.software.search_packages(1, "'; drop") == []


def test_plan_hash(service, user1):
    """The plan hash changes with packages, add-ons and the base image."""
    uid = user1["id"]
    assert service.software.plan(get(service, uid)).hash is None
    service.add_package("user1", uid, "octave")
    h1 = service.software.plan(get(service, uid)).hash
    service.set_addon("user1", uid, "devtools", True)
    h2 = service.software.plan(get(service, uid)).hash
    assert None not in (h1, h2) and h1 != h2
    service.db.execute("UPDATE images SET docker_id = 'sha256:base2'")
    assert service.software.plan(get(service, uid)).hash != h2  # base image rebuilt


def test_user_dockerfile(service, user1):
    """The user Dockerfile installs add-ons in key order, then packages."""
    uid = user1["id"]
    service.set_addon("user1", uid, "vscode", True)
    service.set_addon("user1", uid, "devtools", True)
    service.add_package("user1", uid, "octave")
    text = user_dockerfile(service.software.plan(get(service, uid)))
    lines = text.splitlines()
    assert lines[1] == "FROM ow-desktop:ubuntu-xfce"
    # Add-ons in key order (shared layer cache), then the user's packages.
    assert text.index("add-on: devtools") < text.index("add-on: vscode") < text.index("octave")
    assert "COPY addons/vscode/ /opt/ow/addons/vscode/" in lines
    assert "COPY ow-step /usr/local/sbin/ow-step" in lines
    assert (
        "RUN /usr/local/sbin/ow-step sh -euc "
        "'cd /opt/ow/addons/vscode && sh ./install.sh && rm -rf /var/lib/apt/lists/*'"
    ) in lines
    assert (
        "RUN install -m 644 /opt/ow/addons/vscode/ow-vscode.desktop /usr/share/applications/"
        in lines
    )
    assert any("apt-get update" in line and "gfortran" in line for line in lines)
    assert lines[-1] == 'LABEL ow.managed="true" ow.role="user-image" ow.user="user1"'


def test_image_choice_for_start(service, user1):
    """A start uses the base image, refuses pending changes, then the user image."""
    uid = user1["id"]
    assert service.image_ref_for(get(service, uid)) == "ow-desktop:ubuntu-xfce"
    service.add_package("user1", uid, "octave")
    assert service.software_pending(uid)
    with pytest.raises(DesktopError, match="Press Apply"):
        service.image_ref_for(get(service, uid))
    build_id = service.apply_software("user1", uid)
    assert build_id is not None
    assert service.apply_software("user1", uid) == build_id  # not queued twice
    with pytest.raises(DesktopError, match="being installed"):
        service.image_ref_for(get(service, uid))
    # Simulate a finished build.
    plan = service.software.plan(get(service, uid))
    service.db.execute(
        "UPDATE user_image SET status = 'ready', built_at = now(), pkg_hash = ?, "
        "built_from_image_id = 1, image_ref = 'ow-user-user1:latest'",
        [plan.hash],
    )
    assert not service.software_pending(uid)
    assert service.image_ref_for(get(service, uid)) == "ow-user-user1:latest"
    # A newer selection still starts the last good image until it is applied.
    service.set_addon("user1", uid, "devtools", True)
    assert service.software_pending(uid)
    assert service.image_ref_for(get(service, uid)) == "ow-user-user1:latest"


def test_revoke_switches_off_and_stops_desktop_using_mount(service, user1, monkeypatch):
    """Revoking an add-on switches it off and stops a desktop using it."""
    uid = user1["id"]
    service.grant_addon("boss", uid, "matlab")
    service.set_addon("user1", uid, "matlab", True)
    stopped = []
    monkeypatch.setattr(service.desktops, "addons_in_use", lambda name: {"matlab"})
    monkeypatch.setattr(service.desktops, "stop", lambda user: stopped.append(user["name"]))
    result = service.revoke_addon("boss", uid, "matlab")
    assert stopped == ["user1"]
    assert result.warnings
    assert service.software.active_addons(uid) == []
    assert get(service, uid)["stopped_by_admin"]


def test_addon_mounts_and_env_in_spec(service, settings, user1):
    """Add-on mounts and labels appear in the desktop spec."""
    addons = [service.software.catalog.addons["matlab"]]
    k = build_spec(get(service, user1["id"]), "img", settings, GpuVendor.NONE, "s", addons).kwargs
    mount = k["volumes"][str(settings.root / "shared/matlab")]
    assert mount == {"bind": "/opt/matlab", "mode": "ro"}
    assert k["labels"]["ow.addons"] == "matlab"


def test_start_refuses_missing_shared_install(service, user1):
    """A start is refused when an add-on's shared install is missing."""
    uid = user1["id"]
    service.grant_addon("boss", uid, "matlab")
    service.set_addon("user1", uid, "matlab", True)
    service.db.execute(
        "INSERT INTO user_image VALUES (?, 'ow-user-user1:latest', 1, ?, 'ready', now(), NULL)",
        [uid, service.software.plan(get(service, uid)).hash],
    )
    with pytest.raises(DesktopError, match="MATLAB is not installed on the server"):
        service.start_desktop("user1", uid)


def test_choose_image_drops_unknown_packages_and_rebuilds(service, user1):
    """Choosing another image drops unknown packages and queues a build."""
    uid = user1["id"]
    service.add_package("user1", uid, "octave")
    service.add_package("user1", uid, "gimp")
    service.db.execute(
        "INSERT INTO images (label, base_tag, image_ref, status, built_at, docker_id) "
        "VALUES ('KDE', 'ubuntu-kde', 'ow-desktop:ubuntu-kde', 'ready', now(), 'sha256:kde')"
    )
    service.db.execute("INSERT INTO pkg_index VALUES (2, 'octave')")
    service.desktops.state = lambda name: DesktopState.STOPPED
    service.choose_image("user1", uid, 2)
    assert service.software.packages(uid) == ["octave"]
    assert service.db.scalar("SELECT status FROM user_image WHERE user_id = ?", [uid]) == "queued"
