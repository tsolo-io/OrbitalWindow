<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0046 PostgreSQL for every program, and daily system backups

- Status: accepted (supersedes 0009; changes 0020, 0028 and 0041)
- Date: 2026-11-05

## Context
OrbitalWindow kept its data in a DuckDB file with one writer (ADR 0009). Open WebUI kept its own
SQLite file, and the LLM gateway (ADR 0045) needs PostgreSQL. Martin (#82): if PostgreSQL has to run
for LiteLLM, then everything can use it. There was also no backup setting: `make backup` had to be
put in a crontab by hand. Each node has only a handful of students, so a daily backup is enough;
WAL shipping waits for a multi-node system. The two office servers start clean: no data is moved
from DuckDB.

## Decision
- **One PostgreSQL server, `ow-db`** (`OW_DB_IMAGE`, default `postgres:18.6-alpine3.24`) in
  `deploy/compose.yaml`. Its data is in the volume `ow-db-data`. It is only on its own internal
  network `ow-db`; OrbitalWindow is on it, and OrbitalWindow connects the programs that use it.
- **One database per program.** OrbitalWindow connects as the superuser (`OW_DB_PASSWORD`, made by
  `make init`, or added to an older `.env` by `make start` and `make update`). It makes the other
  databases and their roles (`pg.ensure_database`): Open WebUI gets the role and database
  `openwebui` (`DATABASE_URL`), and the gateway will get `litellm`. Their passwords are in
  `state/db_<role>_password` and are set again at every start.
- **Code.** `db.py` keeps its interface (`execute`, `all`, `one`, `scalar`, `transaction` and `?`
  placeholders, turned into psycopg's `%s`) on one psycopg 3 connection with one lock. The schema
  starts again as one migration. Timestamps stay naive UTC (`SET TIME ZONE 'UTC'`). `numeric`
  results load as int or float, not `Decimal`. A lost connection is opened again; a statement
  outside a transaction is sent once more. The audit log gets an `id`, because `now()` is the same
  for every statement of one transaction. The DuckDB fatal-error exit (ADR 0020) is gone.
- **Tests** use a real server: `OW_TEST_DATABASE_URL` in CI (a service container), else a throwaway
  container (tmpfs, no fsync). Each test gets a copy of a migrated template database.
- **System backups**, set on the admin **Backups** page: on by default, daily at 03:00 (server time
  zone), keep 5. **Back up now** makes one at once. Switching the daily backup off needs a
  confirmation, and then every admin page shows a warning; so does a failed backup. Each backup is
  one file, `$OW_ROOT/backups/ow-backup-<UTC time>.tar` (mode 600, folder 700), made by a one-off
  container from the server's image (so `pg_dump` matches the server) on the database network:
  - every database (`pg_dump -Fc`) and its owner;
  - the CA (ADR 0044), Samba's password database and the SSH gateway's keys (read-only mounts);
  - `state/` without the secrets that are made again by themselves (session key, API keys, role
    passwords);
  - `SHA256SUMS`.

  User data (homes, group folders) is not in it. Admins download backups on the page (audited) or
  copy them with `scp`, and are responsible for keeping copies off the server. `make backup DEST=…`
  copies homes, group folders and the system backups to another disk. `make restore FILE=…`
  restores a system backup on a started stack and starts it again.

## Consequences
- **One more container** (`ow-db`, about 50 MiB) that needs updates; a major version change of
  PostgreSQL needs a dump and restore (a system backup and `make restore`).
- **No more single-writer limits**: `make reset-admin-password` (ADR 0041) and later programs can use
  the database while the service runs.
- **The first start after this change is a new system**: the DuckDB file is not read. The admin
  makes the users again.
- **Backups contain secrets** (the CA key, Samba password hashes), so only admins can download them,
  and the files are readable only by the service's user.
