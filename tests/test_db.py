# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import psycopg
import pytest

from orbitalwindow.db import MIGRATIONS, Database, to_psycopg


def test_migrations_are_idempotent(db_url):
    """Connecting to a migrated database again does not rerun migrations."""
    Database(db_url).close()
    db = Database(db_url)
    assert db.scalar("SELECT max(version) FROM schema_version") == len(MIGRATIONS)
    assert db.scalar("SELECT count(*) FROM schema_version") == len(MIGRATIONS)


def test_transaction_rolls_back(database):
    """An exception inside a transaction rolls it back."""
    with pytest.raises(RuntimeError), database.transaction():
        database.set_setting("a", "1")
        raise RuntimeError
    assert database.get_setting("a") is None


def test_nested_transaction_joins_outer(database):
    """A nested transaction joins the outer one."""
    with database.transaction():
        assert database.next_counter("x") == 1
        assert database.next_counter("x") == 2
    assert database.next_counter("x") == 3


def test_placeholders_and_percent_signs():
    """``?`` becomes ``%s`` and ``%`` is escaped, but not inside quoted text."""
    assert to_psycopg("SELECT ? WHERE a LIKE 'x%?' AND b = ? % 2") == (
        "SELECT %s WHERE a LIKE 'x%%?' AND b = %s %% 2"
    )
    assert to_psycopg('SELECT "a?b" FROM t WHERE c = ?') == 'SELECT "a?b" FROM t WHERE c = %s'


def test_queries_with_and_without_parameters(database):
    """A ``%`` works in queries with and without parameters."""
    database.audit("boss", "a.b", "100%", "")
    assert database.scalar("SELECT count(*) FROM audit WHERE target LIKE '%\\%'") == 1
    assert database.scalar("SELECT count(*) FROM audit WHERE target LIKE ?", ["1%"]) == 1
    assert database.scalar("SELECT 'what?' || ?", ["!"]) == "what?!"


def test_numbers_load_as_int_or_float(database):
    """``sum()`` of integers is an int, other numerics are floats, not Decimal."""
    assert database.scalar("SELECT sum(x) FROM (VALUES (1::bigint), (2::bigint)) v(x)") == 3
    assert database.scalar("SELECT round(2.5::numeric, 1)") == 2.5
    assert type(database.scalar("SELECT sum(x) FROM (VALUES (1), (2)) v(x)")) is int


def test_reconnects_after_a_lost_connection(database):
    """A statement outside a transaction is sent again on a new connection."""
    database.set_setting("a", "1")
    database._con.close()
    assert database.get_setting("a") == "1"


def test_lost_connection_inside_a_transaction_raises(database, pg_server):
    """Inside a transaction a lost connection is an error; the next statement reconnects."""
    with pytest.raises(psycopg.OperationalError), database.transaction():
        database.set_setting("a", "1")
        with psycopg.connect(pg_server, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", [database._con.info.backend_pid])
        database.set_setting("b", "2")
    assert database.get_setting("a") is None
    assert database.get_setting("b") is None


def test_audit_keeps_the_order_inside_a_transaction(database):
    """Entries written in one transaction keep their order (``now()`` is the same for all)."""
    with database.transaction():
        for number in range(5):
            database.audit("boss", f"step.{number}")
    actions = [row["action"] for row in database.all("SELECT action FROM audit ORDER BY id")]
    assert actions == [f"step.{number}" for number in range(5)]
