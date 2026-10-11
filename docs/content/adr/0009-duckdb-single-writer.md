<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0009 DuckDB with one writer process and one lock; UTC timestamps

- Status: accepted
- Date: 2026-10-09

## Context
The design chose DuckDB for simplicity: one file, no database server. DuckDB allows one writing
process. Within a process, a single connection is not safe to use from several threads at once.

## Decision
- Only the service process opens the database (`$OW_ROOT/state/orbitalwindow.duckdb`).
- `db.Database` wraps one connection behind a re-entrant lock. `transaction()` makes a block atomic;
  nested use joins the outer transaction.
- Schema changes are numbered migrations in `db.MIGRATIONS`, applied at startup. A released
  migration is never edited.
- Timestamps are naive UTC `TIMESTAMP` columns (the session time zone is pinned to UTC).
  `TIMESTAMPTZ` results would need `pytz` just to read them. Pages show them in the server's time
  zone (`TZ`) through `ui.common.local_time`; a test stops pages from formatting times directly
  (#32).
- UIDs, MACs and GIDs come from monotonic counters (`counters` table), so numbers are never reused,
  even after a user is deleted.

## Consequences
- Throughput is limited by the lock. That is fine for 2–3 users plus background jobs.
- Tools cannot open the DB while the service runs. Moving to another database only touches `db.py`.