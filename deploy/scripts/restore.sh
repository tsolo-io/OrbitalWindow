#!/bin/sh
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0
#
# Restore a system backup (ow-backup-<time>.tar, made by OrbitalWindow; ADR 0046) on this server:
# the databases, the CA, Samba's passwords, the SSH gateway's keys and state/. It replaces what is
# there now. Homes and group folders are not in a system backup (see make backup).
#
# The stack must have been started once (make start), so the database server and the volumes
# exist. The services that use the restored data are stopped while it runs and started again by
# make start afterwards. Running desktops keep running.
#
# Usage: make restore FILE=<backup>   (in deploy/; or OW_ROOT=… sh scripts/restore.sh <backup>)
set -eu

FILE=${1:?usage: restore.sh <ow-backup-….tar>}
OW_ROOT=${OW_ROOT:-/srv/orbitalwindow}
DB_IMAGE=${OW_DB_IMAGE:-postgres:18.6-alpine3.24}
ENV_FILE=${OW_ENV_FILE:-$(dirname "$0")/../.env}
# From .env, and to docker through the environment: never on a command line.
PGPASSWORD=$(sed -n 's/^OW_DB_PASSWORD=//p' "$ENV_FILE" 2>/dev/null | tail -1)
[ -n "$PGPASSWORD" ] || { echo "OW_DB_PASSWORD is not set in $ENV_FILE" >&2; exit 1; }
export PGPASSWORD
DB_NETWORK=ow-db
STOP="orbitalwindow ow-openwebui ow-samba ow-sshpiper"

[ -f "$FILE" ] || { echo "No such file: $FILE" >&2; exit 1; }
FILE_ABS=$(cd "$(dirname "$FILE")" && pwd)/$(basename "$FILE")
docker inspect -f '{{.State.Running}}' ow-db 2>/dev/null | grep -q true \
    || { echo "The database server (ow-db) is not running. Run make start first." >&2; exit 1; }

echo "Restoring $FILE_ABS"
echo "  stopping: $STOP"
for name in $STOP; do docker stop -t 30 "$name" >/dev/null 2>&1 || true; done

# One container does it all: the backup's own checksums first, then the files, then the databases.
docker run --rm --network "$DB_NETWORK" \
    -e PGHOST=ow-db -e PGUSER=postgres -e PGPASSWORD \
    -v "$FILE_ABS:/backup.tar:ro" \
    -v "$OW_ROOT/state:/state" \
    -v ow-traefik-ca:/ca -v ow-samba-lib:/samba -v ow-sshpiper-keys:/sshpiper \
    "$DB_IMAGE" sh -euc '
work=$(mktemp -d)
tar -C "$work" -xf /backup.tar
cd "$work"
sha256sum -c -s SHA256SUMS || { echo "The backup is damaged (checksums do not match)." >&2; exit 1; }
for part in ca samba sshpiper; do
  find "/$part" -mindepth 1 -delete
  tar -C "/$part" -xzf "$part.tar.gz"
  echo "  restored $part"
done
# state/: the files in the backup replace those here; secrets that are not in it stay.
tar -C /state -xzf state.tar.gz
echo "  restored state"
while read -r database owner; do
  [ -n "$database" ] || continue
  if [ "$owner" != postgres ]; then
    psql -d postgres -qtc "SELECT 1 FROM pg_roles WHERE rolname = '"'"'$owner'"'"'" | grep -q 1 \
      || psql -d postgres -qc "CREATE ROLE \"$owner\" LOGIN"
  fi
  psql -d postgres -qc "DROP DATABASE IF EXISTS \"$database\" WITH (FORCE)"
  psql -d postgres -qc "CREATE DATABASE \"$database\" OWNER \"$owner\""
  pg_restore -d "$database" --no-owner --role="$owner" --exit-on-error "$database.dump"
  echo "  restored database $database"
done < databases
'
echo "Done. Starting the stack again (make start)."
