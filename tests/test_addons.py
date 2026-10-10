# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import pytest
from conftest import ADDONS_DIR

from orbitalwindow.addons import load_addon, load_catalog


def test_shipped_addons_are_valid():
    """The add-ons shipped in the repository load without errors."""
    catalog = load_catalog(ADDONS_DIR)
    assert catalog.errors == []
    assert set(catalog.addons) == {
        "matlab",
        "python-science",
        "vscode",
        "devtools",
        "rstudio",
        "ssh",
    }
    assert catalog.addons["matlab"].spec.restricted
    assert not catalog.addons["vscode"].spec.restricted
    assert all(a.affects_image for a in catalog.addons.values())


def write(folder, yaml_text, files=()):
    """Create an add-on folder with an ``addon.yaml`` and extra files.

    Args:
        folder: Add-on folder to create.
        yaml_text: Content of ``addon.yaml``.
        files: Names of extra files to create in the folder.

    Returns:
        The created folder.
    """
    folder.mkdir(parents=True)
    (folder / "addon.yaml").write_text(yaml_text)
    for name in files:
        (folder / name).write_text("x")
    return folder


GOOD = "key: demo\nlabel: Demo\n"


def test_minimal_addon(tmp_path):
    """A minimal add-on loads and does not affect the image."""
    addon = load_addon(write(tmp_path / "demo", GOOD))
    assert addon.key == "demo" and not addon.affects_image


@pytest.mark.parametrize(
    ("yaml_text", "files", "message"),
    [
        ("key: other\nlabel: X\n", (), "must match the folder name"),
        (GOOD + "apt: ['ok', 'bad name']\n", (), "not valid package names"),
        (GOOD + "apt: ['$(reboot)']\n", (), "not valid package names"),
        (GOOD + "env: {PASSWORD: x}\n", (), "set by OrbitalWindow"),
        (GOOD + "env: {'bad-name': x}\n", (), "not a valid environment variable"),
        (GOOD + "mounts: [{source: /etc, target: /opt/x}]\n", (), "relative to OW_ROOT"),
        (GOOD + "mounts: [{source: shared/../state, target: /opt/x}]\n", (), "without '..'"),
        (GOOD + "mounts: [{source: users/bob, target: /opt/x}]\n", (), "inside shared/"),
        (GOOD + "mounts: [{source: shared/x, target: /config/x}]\n", (), "may not be inside"),
        (GOOD + "mounts: [{source: shared/x, target: relative}]\n", (), "absolute path"),
        (GOOD + "script: install.sh\n", (), "is missing"),
        (GOOD + "script: ../evil.sh\n", (), "file name in the add-on folder"),
        (GOOD + "launchers: [evil.desktop]\n", (), "ow-<name>.desktop"),
        (GOOD + "launchers: [ow-demo.desktop]\n", (), "is missing"),
        (GOOD + "surprise: 1\n", (), "Extra inputs"),
        ("key: [unclosed\n", (), "not valid YAML"),
        ("- a list\n", (), "must be a mapping"),
    ],
)
def test_invalid_addons(tmp_path, yaml_text, files, message):
    """Invalid add-on definitions are refused with a clear message."""
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace("$", r"\$")):
        load_addon(write(tmp_path / "demo", yaml_text, files))


def test_broken_addon_is_skipped_others_load(tmp_path):
    """A broken add-on is reported while the others still load."""
    write(tmp_path / "good", "key: good\nlabel: Good\n")
    write(tmp_path / "bad", "key: bad\nlabel: Bad\napt: ['no way']\n")
    (tmp_path / "empty").mkdir()
    catalog = load_catalog(tmp_path)
    assert list(catalog.addons) == ["good"]
    assert {e.key for e in catalog.errors} == {"bad", "empty"}


def test_files_hash_changes_with_content(tmp_path):
    """Changing an add-on file changes its files hash."""
    folder = write(tmp_path / "demo", GOOD + "script: s.sh\n", ["s.sh"])
    before = load_addon(folder).files_hash
    (folder / "s.sh").write_text("changed")
    assert load_addon(folder).files_hash != before
