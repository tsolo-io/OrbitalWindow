# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Lock-out recovery: ``orbitalwindow-reset-admin [user]`` (#42).

Run by ``make reset-admin-password`` in a one-off container of the service image while the
service is stopped (DuckDB has one writer). It gives the user a new password, enables the account
and makes it an administrator, then prints the password once. Samba is updated through the socket
proxy; a Samba problem is printed as a warning and does not stop the reset.
"""

import logging
import sys

import docker

from .config import get_settings
from .service import Service
from .users import ValidationError, get_user_by_name


def main(argv: list[str] | None = None) -> int:
    """Reset an administrator's password from the command line.

    Args:
        argv: Arguments without the program name: an optional user name (default from the
            settings, ``admin``).

    Returns:
        The exit code: 0 when the password was reset, 1 otherwise.
    """
    # Warnings are printed below; the log shows only errors.
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1 or (args and args[0].startswith("-")):
        print("Usage: orbitalwindow-reset-admin [user]", file=sys.stderr)
        return 1
    settings = get_settings()
    name = args[0] if args else settings.admin_name
    # Docker (through the socket proxy) is only needed for Samba; it connects on first use.
    service = Service(settings, docker.from_env(), prepare_layout=False)
    existed = get_user_by_name(service.db, name) is not None
    try:
        result = service.recover_admin(name)
    except ValidationError as exc:
        print(f"The password was not reset: {exc}", file=sys.stderr)
        return 1
    finally:
        service.db.close()
    for warning in result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    done = "is an enabled administrator again" if existed else "was created as an administrator"
    print(f"User {name} {done}. New password (shown once):")
    print(f"    {result.password}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
