# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""SSH keys and gateway labels; built-in help pages."""

import base64
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from orbitalwindow import ssh
from orbitalwindow.desktops import GpuVendor, SshAccess, build_spec
from orbitalwindow.help import list_pages, render
from orbitalwindow.users import ValidationError

HELP_DIR = Path(__file__).parent.parent / "docs/content/user"


def pub(key) -> str:
    """Return the OpenSSH public key line of a private key.

    Args:
        key: cryptography private key.

    Returns:
        The public key in OpenSSH format.
    """
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )


ED = pub(ed25519.Ed25519PrivateKey.generate())
ECDSA = pub(ec.generate_private_key(ec.SECP256R1()))
RSA_SMALL = pub(rsa.generate_private_key(65537, 2048))
RSA_OK = pub(rsa.generate_private_key(65537, 3072))


@pytest.mark.parametrize("line", [ED, ECDSA, RSA_OK, ED + " me@laptop", "  " + ED + "\n"])
def test_valid_keys(line):
    """Supported public keys parse with a SHA256 fingerprint."""
    key = ssh.parse_public_key(line)
    assert key.fingerprint.startswith("SHA256:") and key.line.split()[0] == key.type


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (RSA_SMALL, "at least 3072"),
        ("ssh-dss AAAAB3NzaC1kc3MAAACBAP", "Unsupported key type"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----", "private"),
        ("ssh-ed25519 not*base64", "damaged"),
        ("ssh-rsa " + ED.split()[1], "does not match"),
        (ED + "\n" + ECDSA, "single line"),
        ("", "single line"),
    ],
)
def test_invalid_keys(text, message):
    """Weak, unsupported, private, damaged or multi-line keys are refused."""
    with pytest.raises(ValidationError, match=message):
        ssh.parse_public_key(text)


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen not installed")
def test_fingerprint_matches_ssh_keygen(tmp_path):
    """The fingerprint matches ssh-keygen's."""
    (tmp_path / "k.pub").write_text(ED + "\n")
    out = subprocess.run(["ssh-keygen", "-lf", str(tmp_path / "k.pub")], capture_output=True,
                         text=True, check=True).stdout  # fmt: skip
    assert out.split()[1] == ssh.parse_public_key(ED).fingerprint


def test_upstream_key_is_created_once(tmp_path):
    """The upstream key is created once, private, and can be removed."""
    private, public = ssh.ensure_upstream_key(tmp_path, "user1")
    assert private.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert public.startswith("ssh-ed25519 ") and public.endswith("ow-upstream-user1")
    assert (tmp_path / "ssh/user1").stat().st_mode & 0o777 == 0o600
    assert ssh.ensure_upstream_key(tmp_path, "user1") == (private, public)
    ssh.remove_upstream_key(tmp_path, "user1")
    assert not (tmp_path / "ssh/user1").exists()


def test_sshpiper_labels():
    """sshpiper labels route to the desktop without a root exec command."""
    labels = ssh.sshpiper_labels("user1", [ED.split(" ")[0] + " " + ED.split(" ")[1]], "PRIV")
    assert labels["sshpiper.username"] == "user1"
    assert labels["sshpiper.container_username"] == "abc"
    assert labels["sshpiper.port"] == "2222"
    assert labels["sshpiper.network"] == "ow-net-user1"
    assert base64.b64decode(labels["sshpiper.private_key"]) == b"PRIV"
    assert base64.b64decode(labels["sshpiper.authorized_keys"]).decode().startswith("ssh-ed25519 ")
    assert "sshpiper.docker_exec_cmd" not in labels  # would give root shells


def test_key_management(service, users):
    """Users can add, list and remove only their own SSH keys."""
    uid = users["user1"]["id"]
    added = service.add_ssh_key("user1", uid, "", ED + " me@laptop")
    assert added["label"] == "me@laptop"
    with pytest.raises(ValidationError, match="already added"):
        service.add_ssh_key("user1", uid, "again", ED)
    service.add_ssh_key("user1", uid, "Work PC", ECDSA)
    keys = service.ssh_keys(uid)
    assert [k["label"] for k in keys] == ["me@laptop", "Work PC"]
    assert "me@laptop" not in keys[0]["pubkey"]  # comments are not passed on
    with pytest.raises(ValidationError):
        service.add_ssh_key("user1", uid, "<script>", RSA_OK)
    service.remove_ssh_key("user1", uid, keys[0]["id"])
    with pytest.raises(ValidationError):  # someone else's key id
        service.remove_ssh_key("user2", users["user2"]["id"], keys[1]["id"])
    assert len(service.ssh_keys(uid)) == 1


def test_desktop_spec_with_ssh(service, settings, users):
    """A user with SSH keys gets the upstream key mount and sshpiper labels."""
    user = service.db.one("SELECT * FROM users WHERE name = 'user1'")
    service.add_ssh_key("user1", user["id"], "k", ED)
    access = service._ssh_access(user)
    assert isinstance(access, SshAccess)
    assert access.upstream_pub == settings.root / "state/ssh/user1.pub"
    spec = build_spec(user, "img", settings, GpuVendor.NONE, "s", ssh=access)
    assert spec.kwargs["volumes"][str(access.upstream_pub)] == {
        "bind": "/etc/ow/upstream.pub",
        "mode": "ro",
    }
    assert spec.kwargs["labels"]["sshpiper.username"] == "user1"
    plain = build_spec(user, "img", settings, GpuVendor.NONE, "s")
    assert "sshpiper.username" not in plain.kwargs["labels"]


def test_ssh_command(service):
    """The SSH command uses the configured hostname and gateway port."""
    service.db.set_setting("hostname", "desk.example.org")
    assert service.ssh_command("user1") == "ssh -p 2222 user1@desk.example.org"


def test_help_pages_are_ordered_and_filled_in():
    """Help pages are ordered and their placeholders and links filled in."""
    pages = list_pages(HELP_DIR)
    slugs = ["getting-started", "software", "files", "ssh", "help", "ai", "terminal", "environment"]
    assert [p.slug for p in pages] == slugs
    assert pages[0].title == "Getting started"
    values = {"host": "desk.x", "user": "user1", "samba_path": r"\\desk.x\user1",
              "ssh_cmd": "ssh -p 2222 user1@desk.x", "groups": "**labx**"}  # fmt: skip
    text = render(next(p for p in pages if p.slug == "files"), values)
    assert "SPDX" not in text  # licence comment removed
    assert r"`\\desk.x\user1`" in text and "**labx**" in text
    assert "{host}" not in text and "{user}" not in text
    first = render(pages[0], values)
    assert "](/help/software)" in first  # links between pages point at /help
    assert "ssh -p 2222 user1@desk.x" in render(pages[3], values)


def test_help_page_requires_login(client):
    """The API app exposes no unauthenticated help route."""
    r = client.get("/help", follow_redirects=False)
    # The UI is not mounted in the API test app; /help is a NiceGUI page. Just make sure the API
    # app has no unauthenticated help route that could leak anything.
    assert r.status_code == 404
