#!/bin/sh
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0
set -eu
# Recreate Unix users and shares from users.json (the container filesystem is not persistent;
# /var/lib/samba with the password database is a volume).
ow-sync --no-reload || echo "ow-sync failed; starting with no user shares" >&2
exec smbd --foreground --no-process-group --debug-stdout
