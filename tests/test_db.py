# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from orbitalwindow.db import MIGRATIONS, Database


def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "ow.duckdb"
    Database(path).close()
    db = Database(path)
    assert db.scalar("SELECT max(version) FROM schema_version") == len(MIGRATIONS)


def test_transaction_rolls_back():
    db = Database(":memory:")
    with pytest.raises(RuntimeError), db.transaction():
        db.set_setting("a", "1")
        raise RuntimeError
    assert db.get_setting("a") is None


def test_nested_transaction_joins_outer():
    db = Database(":memory:")
    with db.transaction():
        assert db.next_counter("x") == 1
        assert db.next_counter("x") == 2
    assert db.next_counter("x") == 3


def test_fatal_error_exits_the_process(monkeypatch):
    import duckdb

    db = Database(":memory:")
    calls = []
    db.on_fatal = lambda: calls.append("exit")

    class Broken:
        def execute(self, *args):
            raise duckdb.FatalException("database has been invalidated")

    db._con = Broken()
    with pytest.raises(duckdb.FatalException):
        db.scalar("SELECT 1")
    assert calls == ["exit"]


def test_pkg_index_has_no_secondary_index():
    db = Database(":memory:")
    assert db.scalar("SELECT count(*) FROM duckdb_indexes() WHERE table_name = 'pkg_index'") == 0
