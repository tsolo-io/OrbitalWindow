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
