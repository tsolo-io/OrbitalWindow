# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from orbitalwindow.db import MIGRATIONS, Database


def test_migrations_are_idempotent(tmp_path):
    """Opening an existing database again does not rerun migrations."""
    path = tmp_path / "ow.duckdb"
    Database(path).close()
    db = Database(path)
    assert db.scalar("SELECT max(version) FROM schema_version") == len(MIGRATIONS)


def test_transaction_rolls_back():
    """An exception inside a transaction rolls it back."""
    db = Database(":memory:")
    with pytest.raises(RuntimeError), db.transaction():
        db.set_setting("a", "1")
        raise RuntimeError
    assert db.get_setting("a") is None


def test_nested_transaction_joins_outer():
    """A nested transaction joins the outer one."""
    db = Database(":memory:")
    with db.transaction():
        assert db.next_counter("x") == 1
        assert db.next_counter("x") == 2
    assert db.next_counter("x") == 3


def test_fatal_error_exits_the_process(monkeypatch):
    """A DuckDB fatal error calls the on_fatal hook and is re-raised."""
    import duckdb

    db = Database(":memory:")
    calls = []
    db.on_fatal = lambda: calls.append("exit")

    class Broken:
        def execute(self, *args):
            """Raise a DuckDB fatal error for every query.

            Args:
                *args: Query arguments (ignored).

            Raises:
                duckdb.FatalException: Always.
            """
            raise duckdb.FatalException("database has been invalidated")

    db._con = Broken()
    with pytest.raises(duckdb.FatalException):
        db.scalar("SELECT 1")
    assert calls == ["exit"]


def test_pkg_index_has_no_secondary_index():
    """The package index table has no secondary index."""
    db = Database(":memory:")
    assert db.scalar("SELECT count(*) FROM duckdb_indexes() WHERE table_name = 'pkg_index'") == 0


@pytest.mark.parametrize("start", range(1, len(MIGRATIONS)))
def test_migrations_apply_to_a_database_with_rows(tmp_path, monkeypatch, start):
    """Each migration also applies to a database that already has an image row."""
    path = tmp_path / "ow.duckdb"
    monkeypatch.setattr("orbitalwindow.db.MIGRATIONS", MIGRATIONS[:start])
    db = Database(path)
    columns = db._con.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'images' AND is_nullable = 'NO' AND column_default IS NULL"
    ).fetchall()
    names = [row[0] for row in columns]
    if names:
        db._con.execute(
            f"INSERT INTO images ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            ["x"] * len(names),
        )
    db.close()
    monkeypatch.setattr("orbitalwindow.db.MIGRATIONS", MIGRATIONS)
    db = Database(path)
    assert db.scalar("SELECT max(version) FROM schema_version") == len(MIGRATIONS)
