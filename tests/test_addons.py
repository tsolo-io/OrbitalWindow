# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import pytest
from conftest import ADDONS_DIR

from orbitalwindow.addons import load_addon, load_catalog


def test_shipped_addons_are_valid():
    catalog = load_catalog(ADDONS_DIR)
    assert catalog.errors == []
    assert set(catalog.addons) == {"matlab", "python-science", "vscode", "devtools", "rstudio"}
    assert catalog.addons["matlab"].spec.restricted
    assert not catalog.addons["vscode"].spec.restricted
    assert all(a.affects_image for a in catalog.addons.values())


def write(folder, yaml_text, files=()):
    folder.mkdir(parents=True)
    (folder / "addon.yaml").write_text(yaml_text)
    for name in files:
        (folder / name).write_text("x")
    return folder


GOOD = "key: demo\nlabel: Demo\n"


def test_minimal_addon(tmp_path):
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
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace("$", r"\$")):
        load_addon(write(tmp_path / "demo", yaml_text, files))


def test_broken_addon_is_skipped_others_load(tmp_path):
    write(tmp_path / "good", "key: good\nlabel: Good\n")
    write(tmp_path / "bad", "key: bad\nlabel: Bad\napt: ['no way']\n")
    (tmp_path / "empty").mkdir()
    catalog = load_catalog(tmp_path)
    assert list(catalog.addons) == ["good"]
    assert {e.key for e in catalog.errors} == {"bad", "empty"}


def test_files_hash_changes_with_content(tmp_path):
    folder = write(tmp_path / "demo", GOOD + "script: s.sh\n", ["s.sh"])
    before = load_addon(folder).files_hash
    (folder / "s.sh").write_text("changed")
    assert load_addon(folder).files_hash != before
