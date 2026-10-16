# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Other distributions: add-ons per package manager, user images and package names."""

import subprocess
from pathlib import Path

import pytest

from orbitalwindow.addons import AddonSpec, load_catalog
from orbitalwindow.builds import user_dockerfile
from orbitalwindow.users import ValidationError

ADDONS = Path(__file__).parent.parent / "addons"
OW_PKG = Path(__file__).parent.parent / "deploy/image/root/usr/local/bin/ow-pkg"
BASE_PACKAGES = Path(__file__).parent.parent / "deploy/image/base-packages.sh"


def spec(**fields) -> AddonSpec:
    """Return an add-on spec with a key and label.

    Args:
        **fields: Further ``addon.yaml`` fields.

    Returns:
        The spec.
    """
    return AddonSpec(key="demo", label="Demo", **fields)


@pytest.mark.parametrize(
    ("fields", "supported"),
    [
        ({}, {"apt", "apk", "dnf", "pacman"}),  # mounts or environment only
        ({"apt": ["aa"]}, {"apt"}),
        ({"apt": ["aa"], "dnf": ["Ab"]}, {"apt", "dnf"}),
        ({"script": "install.sh"}, {"apt"}),  # scripts are assumed to be for Ubuntu
        ({"script": "install.sh", "managers": ["apt", "pacman"]}, {"apt", "pacman"}),
        ({"apt": ["aa"], "managers": ["apk"]}, {"apk"}),
    ],
)
def test_supports(fields, supported):
    """An add-on works where it lists packages, unless ``managers`` says otherwise."""
    addon = spec(**fields)
    assert {m for m in ("apt", "apk", "dnf", "pacman") if addon.supports(m)} == supported


def test_package_lists_are_checked():
    """Package names and manager names are validated."""
    assert spec(dnf=["ImageMagick", "gcc-c++"]).packages_for("dnf") == ["ImageMagick", "gcc-c++"]
    with pytest.raises(ValueError, match="not valid package names"):
        spec(pacman=["bad name"])
    with pytest.raises(ValueError, match="unknown package managers"):
        spec(managers=["yum"])


def test_shipped_addons_per_system():
    """SSH and the developer tools work everywhere; conda not on Alpine; VS Code on apt only."""
    catalog = load_catalog(ADDONS)
    assert not catalog.errors
    addons = {key: addon.spec for key, addon in catalog.addons.items()}
    for manager in ("apt", "apk", "dnf", "pacman"):
        assert addons["ssh"].supports(manager) and addons["devtools"].supports(manager)
    assert not addons["python-science"].supports("apk")
    assert addons["python-science"].supports("dnf")
    assert {m for m in ("apt", "apk", "dnf", "pacman") if addons["vscode"].supports(m)} == {"apt"}


def fedora_image(service) -> int:
    """Add a ready Fedora terminal-only image with a package index.

    Args:
        service: Service under test.

    Returns:
        The image id.
    """
    image_id = service.db.scalar(
        "INSERT INTO images (label, base_tag, image_ref, status, built_at, docker_id, gui, "
        "pkg_manager) VALUES ('Fedora', 'baseimage-fedora-42', 'ow-terminal:baseimage-fedora-42', "
        "'ready', now(), 'sha256:f', false, 'dnf') RETURNING id"
    )
    service.db.execute(
        "INSERT INTO pkg_index SELECT ?, unnest(['ImageMagick', 'imagemagick-libs', 'octave'])",
        [image_id],
    )
    return image_id


def test_addons_not_for_the_system_are_greyed_out(service, users):
    """On Fedora, apt-only add-ons are unavailable and drop out of the image plan."""
    uid = users["user1"]["id"]
    service.set_addon("user1", uid, "rstudio", True)
    service.db.execute("UPDATE users SET image_id = ? WHERE id = ?", [fedora_image(service), uid])
    states = {s.addon.key: s for s in service.software.addon_states(uid)}
    assert states["rstudio"].unsupported == "Fedora" and not states["rstudio"].active
    assert not states["ssh"].unsupported
    with pytest.raises(ValidationError, match="not available for Fedora"):
        service.set_addon("user1", uid, "rstudio", True)
    service.set_addon("user1", uid, "ssh", True)
    plan = service.software.plan(service.db.one("SELECT * FROM users WHERE id = ?", [uid]))
    assert [a.key for a in plan.addons] == ["ssh"] and plan.manager == "dnf"
    text = user_dockerfile(plan)
    assert "ow-pkg install openssh-server" in text and "OW_PKG_MANAGER=dnf" in text
    assert "apt-get" not in text


def test_package_names_keep_their_case(service, users):
    """Package names match without regard to case, and are stored as the system spells them."""
    uid = users["user1"]["id"]
    image_id = fedora_image(service)
    service.db.execute("UPDATE users SET image_id = ? WHERE id = ?", [image_id, uid])
    assert service.software.search_packages(image_id, "image") == [
        "ImageMagick",
        "imagemagick-libs",
    ]
    assert service.add_package("user1", uid, "imagemagick") == "ImageMagick"
    assert service.add_package("user1", uid, "ImageMagick") == "ImageMagick"
    assert service.software.packages(uid) == ["ImageMagick"]


def test_index_records_the_package_manager(service):
    """The index's first line is the package manager; the rest are package names."""
    service.client.containers.run.return_value = b"pacman\nbase-devel\ncmake\n"
    assert service.builder._index_packages(1, "ow-desktop:arch-xfce") == 2
    assert service.db.scalar("SELECT pkg_manager FROM images WHERE id = 1") == "pacman"
    command = service.client.containers.run.call_args.args[1]
    assert command == ["-c", "ow-pkg manager && ow-pkg index"]


def test_scripts_are_valid_shell():
    """ow-pkg and the base package script are valid shell, and ow-pkg finds a manager."""
    for script in (OW_PKG, BASE_PACKAGES):
        subprocess.run(["sh", "-n", str(script)], check=True)
    out = subprocess.run(["sh", str(OW_PKG), "manager"], capture_output=True, text=True)
    assert out.stdout.strip() in {"apt", "apk", "dnf", "pacman", "unknown"}
