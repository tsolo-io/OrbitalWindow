# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""SSH/SFTP access through the sshpiper gateway.

``ssh -p 2222 <user>@host`` reaches sshpiper, which checks the user's public keys (from the
desktop's ``sshpiper.authorized_keys`` label) and then logs in to the desktop's own sshd as ``abc``
with a per-user *upstream* key that only OrbitalWindow and that desktop know.
"""

import base64
import binascii
import hashlib
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .users import ValidationError

SSH_ADDON = "ssh"
SSH_PORT = 2222  # sshd inside the desktop, and the gateway on the host
MAX_KEYS = 20
ALLOWED_TYPES = frozenset(
    {
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
        "ssh-rsa",
    }
)
MIN_RSA_BITS = 3072
LABEL_RE = re.compile(r"^[\w .@()+-]{0,60}$")


@dataclass(frozen=True)
class PublicKey:
    type: str
    blob: bytes
    comment: str

    @property
    def fingerprint(self) -> str:
        """Return the OpenSSH SHA-256 fingerprint of the key.

        Returns:
            ``SHA256:<base64 digest>``.
        """
        digest = base64.b64encode(hashlib.sha256(self.blob).digest()).decode().rstrip("=")
        return f"SHA256:{digest}"

    @property
    def line(self) -> str:
        """Return the key without its comment (comments are user text; labels hold the name).

        Returns:
            ``<type> <base64 blob>``.
        """
        return f"{self.type} {base64.b64encode(self.blob).decode()}"


def _read_string(data: bytes, offset: int) -> tuple[bytes, int]:
    """Read one length-prefixed SSH wire-format string.

    Args:
        data: The key blob.
        offset: Where the 4-byte length starts.

    Returns:
        The string bytes and the offset just after them.

    Raises:
        ValueError: If the data ends too early.
    """
    if offset + 4 > len(data):
        raise ValueError("truncated")
    (length,) = struct.unpack(">I", data[offset : offset + 4])
    end = offset + 4 + length
    if end > len(data):
        raise ValueError("truncated")
    return data[offset + 4 : end], end


def parse_public_key(text: str) -> PublicKey:
    """Validate one OpenSSH public key line, e.g. the contents of ``id_ed25519.pub``.

    Args:
        text: The pasted key.

    Returns:
        The parsed key.

    Raises:
        ValidationError: If the text is a private key, not a single key line, an unsupported
            type, damaged, or an RSA key that is too short.
    """
    if "PRIVATE KEY" in text:
        raise ValidationError("That is a private key. Never share it; paste the .pub file.")
    parts = text.strip().split(None, 2)
    if len(parts) < 2 or "\n" in text.strip():
        raise ValidationError(
            "Paste one public key: a single line starting with ssh-ed25519, ssh-rsa or ecdsa-…"
        )
    key_type, b64 = parts[0], parts[1]
    if key_type not in ALLOWED_TYPES:
        raise ValidationError(f"Unsupported key type '{key_type[:40]}'. Use ssh-ed25519.")
    try:
        blob = base64.b64decode(b64, validate=True)
        inner, offset = _read_string(blob, 0)
    except binascii.Error, ValueError:
        raise ValidationError("The key is damaged (it is not valid base64).") from None
    if inner.decode(errors="replace") != key_type:
        raise ValidationError("The key is damaged (its type does not match).")
    if key_type == "ssh-rsa":
        _, offset = _read_string(blob, offset)  # public exponent
        modulus, _ = _read_string(blob, offset)
        bits = (len(modulus.lstrip(b"\0"))) * 8
        if bits < MIN_RSA_BITS:
            raise ValidationError(f"RSA keys must have at least {MIN_RSA_BITS} bits.")
    comment = parts[2].strip() if len(parts) > 2 else ""
    return PublicKey(key_type, blob, comment)


def validate_label(label: str, key: PublicKey) -> str:
    """Return a valid name for a key, falling back to its comment or ``key``.

    Args:
        label: The name the user gave, possibly empty.
        key: The parsed key.

    Returns:
        The trimmed name, at most 60 characters.

    Raises:
        ValidationError: If the name contains characters that are not allowed.
    """
    label = (label or key.comment or "key").strip()[:60]
    if not LABEL_RE.fullmatch(label):
        raise ValidationError("The name may contain letters, digits, spaces and . @ ( ) + -")
    return label


def ensure_upstream_key(state_dir: Path, user: str) -> tuple[str, str]:
    """Return the per-user key sshpiper uses to log in to the desktop. Created once (mode 600).

    Args:
        state_dir: The service state folder; keys live in its ``ssh`` sub-folder.
        user: OrbitalWindow user name.

    Returns:
        ``(private_key_openssh, public_key_line)``.
    """
    folder = state_dir / "ssh"
    folder.mkdir(parents=True, exist_ok=True)
    private_path, public_path = folder / user, folder / f"{user}.pub"
    if not private_path.exists():
        key = Ed25519PrivateKey.generate()
        private = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        public = key.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        )
        fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(private)
        public_path.write_bytes(public + f" ow-upstream-{user}\n".encode())
        public_path.chmod(0o644)
    return private_path.read_text(), public_path.read_text().strip()


def remove_upstream_key(state_dir: Path, user: str) -> None:
    """Delete a user's upstream key pair, if present.

    Args:
        state_dir: The service state folder.
        user: OrbitalWindow user name.
    """
    for path in (state_dir / "ssh" / user, state_dir / "ssh" / f"{user}.pub"):
        path.unlink(missing_ok=True)


def sshpiper_labels(user: str, authorized_keys: list[str], private_key: str) -> dict[str, str]:
    """Return the labels on the desktop container that tell sshpiper how to route ``user``.

    Args:
        user: OrbitalWindow user name.
        authorized_keys: The user's public key lines.
        private_key: The upstream private key (OpenSSH format).

    Returns:
        The ``sshpiper.*`` labels.
    """

    def b64(text: str) -> str:
        """Base64-encode text.

        Args:
            text: The text to encode.

        Returns:
            The base64 string.
        """
        return base64.b64encode(text.encode()).decode()

    return {
        "sshpiper.username": user,
        "sshpiper.container_username": "abc",
        "sshpiper.port": str(SSH_PORT),
        "sshpiper.network": f"ow-net-{user}",
        "sshpiper.authorized_keys": b64("\n".join(authorized_keys) + "\n"),
        "sshpiper.private_key": b64(private_key),
    }
