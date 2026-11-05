# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The PostgreSQL server ``ow-db`` (ADR 0046): one server, one database per program.

OrbitalWindow connects as the server's superuser (``OW_DB_PASSWORD``): it makes the other programs'
databases and roles, and it runs the backups. Every other program (Open WebUI, later the LLM
gateway) gets its own role that owns only its own database. Their passwords are kept in ``state/``
and set again at every start, so a lost file only means a new password.
"""

import logging
import secrets
from pathlib import Path

import psycopg
from psycopg import sql

from .config import Settings

log = logging.getLogger(__name__)

# Docker network of the database server (deploy/compose.yaml); programs that use it join it.
NETWORK = "ow-db"


def role_password(settings: Settings, role: str) -> str:
    """Return the password of a program's database role, making one the first time.

    Args:
        settings: Application settings (state folder).
        role: The role name, e.g. ``openwebui``.

    Returns:
        The password.
    """
    path: Path = settings.state_dir / f"db_{role}_password"
    try:
        password = path.read_text().strip()
    except FileNotFoundError:
        password = ""
    if not password:
        password = secrets.token_hex(24)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(password + "\n")
        path.chmod(0o600)
    return password


def ensure_database(settings: Settings, name: str, owner: str | None = None) -> None:
    """Make a database (and its owner role) when they don't exist yet.

    Args:
        settings: Application settings (the server and its superuser).
        name: Database name.
        owner: Role that owns the database, made or given its password from ``state/``; None
            for a database of OrbitalWindow itself (owned by the superuser).
    """
    with psycopg.connect(settings.database_url("postgres"), autocommit=True) as con:
        if owner is not None:
            password = role_password(settings, owner)
            exists = con.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [owner]).fetchone()
            verb = "ALTER" if exists else "CREATE"
            con.execute(
                sql.SQL(verb + " ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(owner), sql.Literal(password)
                )
            )
        if not con.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name]).fetchone():
            log.info("creating the database %s", name)
            con.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(name), sql.Identifier(owner or settings.db_user)
                )
            )


def program_url(settings: Settings, name: str) -> str:
    """Return the URL a program uses for its own database, as its own role.

    Args:
        settings: Application settings (the server).
        name: The program's database and role name.

    Returns:
        ``postgresql://<name>:<password>@<host>:<port>/<name>``.
    """
    password = role_password(settings, name)
    return f"postgresql://{name}:{password}@{settings.db_host}:{settings.db_port}/{name}"
