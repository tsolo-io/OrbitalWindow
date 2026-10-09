#!/bin/sh
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0
#
# Back up what can't be recreated: homes (users/), group folders (groups/), state/ (DuckDB,
# session key, SSH upstream keys, file manager data) and the Samba password database. Shared
# software (shared/matlab, shared/conda) is reinstallable and not included.
#
# Runs everything through Docker, so it works for any member of the docker group (files belong to
# many UIDs). The OrbitalWindow service is stopped for a few seconds while state/ is copied, so the
# DuckDB file is consistent; running desktops are not affected.
#
# Usage: OW_ROOT=/srv/orbitalwindow scripts/backup.sh <backup folder> [days to keep, default 14]
set -eu

DEST=${1:?usage: backup.sh <backup folder> [days to keep]}
KEEP_DAYS=${2:-14}
OW_ROOT=${OW_ROOT:-/srv/orbitalwindow}
SERVICE=${OW_SERVICE_CONTAINER:-orbitalwindow}
SAMBA_VOLUME=${OW_SAMBA_VOLUME:-ow-samba-lib}
HELPER=${OW_HELPER_IMAGE:-alpine:3.22}
STAMP=$(date +%Y%m%d-%H%M%S)
TARGET="$DEST/ow-backup-$STAMP"

mkdir -p "$TARGET"
DEST_ABS=$(cd "$DEST" && pwd)
echo "Backing up $OW_ROOT to $TARGET"

archive() {  # archive <name> <docker -v source> <path inside /src> [tar options…]
    name=$1 src=$2 path=$3
    shift 3
    docker run --rm --network none \
        -v "$src:/src:ro" -v "$DEST_ABS/ow-backup-$STAMP:/dst" \
        "$HELPER" tar -C /src -czf "/dst/$name.tar.gz" "$@" "$path"
    echo "  $name.tar.gz $(du -h "$TARGET/$name.tar.gz" | cut -f1)"
}

archive users "$OW_ROOT" users
archive groups "$OW_ROOT" groups

was_running=$(docker inspect -f '{{.State.Running}}' "$SERVICE" 2>/dev/null || echo false)
if [ "$was_running" = true ]; then
    docker stop -t 30 "$SERVICE" >/dev/null
fi
# Always start the service again, even if the copy fails.
trap '[ "$was_running" = true ] && docker start "$SERVICE" >/dev/null' EXIT
archive state "$OW_ROOT" state
if [ "$was_running" = true ]; then
    docker start "$SERVICE" >/dev/null
fi
trap - EXIT

# msg.sock holds Samba's runtime sockets, not data.
archive samba "$SAMBA_VOLUME" . --exclude=./private/msg.sock

# Checksums, then remove old backups.
(cd "$TARGET" && sha256sum ./*.tar.gz > SHA256SUMS)
find "$DEST_ABS" -maxdepth 1 -type d -name 'ow-backup-*' -mtime +"$KEEP_DAYS" -print \
    -exec rm -rf {} + | sed 's/^/  removed old backup /'
echo "Done: $TARGET"
