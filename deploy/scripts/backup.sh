#!/bin/sh
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0
#
# Copy the user data and the system backups to another place (DEST, for example a disk or a
# network share): homes (users/), group folders (groups/), and the system backups that
# OrbitalWindow makes every day in $OW_ROOT/backups (databases, CA, Samba passwords, SSH keys;
# ADR 0046). Shared software (shared/matlab, shared/conda) is reinstallable and not included.
#
# Runs everything through Docker, so it works for any member of the docker group (files belong to
# many UIDs). Nothing is stopped.
#
# Usage: make backup DEST=<backup folder> [KEEP=14]   (in deploy/; or OW_ROOT=… sh scripts/backup.sh <folder> [days])
set -eu

DEST=${1:?usage: backup.sh <backup folder> [days to keep]}
KEEP_DAYS=${2:-14}
OW_ROOT=${OW_ROOT:-/srv/orbitalwindow}
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
# The system backups are already compressed; restore one with make restore FILE=….
archive system-backups "$OW_ROOT" backups

# Checksums, then remove old backups.
(cd "$TARGET" && sha256sum ./*.tar.gz > SHA256SUMS)
find "$DEST_ABS" -maxdepth 1 -type d -name 'ow-backup-*' -mtime +"$KEEP_DAYS" -print \
    -exec rm -rf {} + | sed 's/^/  removed old backup /'
echo "Done: $TARGET"
