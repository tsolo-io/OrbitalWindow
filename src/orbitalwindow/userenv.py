# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""A user's own environment variables, set in every desktop they start (ADR 0032).

They are stored per user, not per desktop, so they stay when the user switches desktops. They
apply at the next start: the desktop's ``ow.env`` label holds a hash of the variables it was
started with, so the pages can tell when a restart is needed.
"""

import hashlib
import json
import re
from collections.abc import Mapping

from .addons import RESERVED_ENV
from .users import ValidationError

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_VARS = 50
MAX_VALUE = 4096
LABEL_ENV = "ow.env"
NAMES_VAR = "OW_USER_ENV"  # names of the user's variables, for the SSH profile script
# Besides what the desktop spec sets: variables the container's start-up and logins rely on.
RESERVED = RESERVED_ENV | {"PATH", "HOME", "USER", "SHELL", "HOSTNAME", "LD_PRELOAD"}
RESERVED_PREFIXES = ("OW_", "S6_", "LSIO_", "NVIDIA_")


def validate_name(name: str) -> str:
    """Check a variable name.

    Args:
        name: The name as entered.

    Returns:
        The name without surrounding spaces.

    Raises:
        ValidationError: When the name is not a valid shell variable name or is reserved.
    """
    name = name.strip()
    if not NAME_RE.fullmatch(name):
        raise ValidationError(
            "A name may contain letters, digits and _ (not first), up to 64 characters."
        )
    if name.upper() in RESERVED or name.upper().startswith(RESERVED_PREFIXES):
        raise ValidationError(f"{name} is set by OrbitalWindow and can't be changed here.")
    return name


def validate_value(value: str) -> str:
    """Check a variable's value.

    Args:
        value: The value as entered (kept as is, spaces included).

    Returns:
        The value.

    Raises:
        ValidationError: When it is too long or has a line break.
    """
    if len(value) > MAX_VALUE:
        raise ValidationError(f"A value may be at most {MAX_VALUE} characters.")
    if any(char in value for char in "\r\n\0"):
        raise ValidationError("A value must be a single line.")
    return value


def env_hash(env: Mapping[str, str]) -> str:
    """Return a short hash of a set of variables, for the desktop's ``ow.env`` label.

    Args:
        env: The variables.

    Returns:
        16 hex characters; the same for the same names and values in any order.
    """
    data = json.dumps(sorted(env.items()), separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()[:16]
