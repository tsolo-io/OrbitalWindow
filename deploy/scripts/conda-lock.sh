#!/bin/sh
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0
#
# For maintainers: resolve deploy/conda/science.yml with the pinned Miniforge and write
# deploy/conda/science.lock, the exact package list servers install (#68, ADR 0047). Commit both.
# Runs in a throwaway container; needs Docker and internet.
#
# Usage: sh deploy/scripts/conda-lock.sh
set -eu

HERE=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source-path=SCRIPTDIR source=../conda/miniforge.env
. "$HERE/conda/miniforge.env"

docker run --rm -i --network bridge \
    -e MINIFORGE_VERSION="$MINIFORGE_VERSION" -e MINIFORGE_SHA256="$MINIFORGE_SHA256" \
    -v "$HERE/conda:/conda" \
    ubuntu:26.04 bash -eu <<'INSIDE'
apt-get update -qq && apt-get install -y -qq --no-install-recommends curl ca-certificates >/dev/null
curl -fsSL -o /tmp/miniforge.sh "https://github.com/conda-forge/miniforge/releases/download/$MINIFORGE_VERSION/Miniforge3-$MINIFORGE_VERSION-Linux-x86_64.sh"
echo "$MINIFORGE_SHA256  /tmp/miniforge.sh" | sha256sum -c -
bash /tmp/miniforge.sh -b -p /opt/conda >/dev/null
/opt/conda/bin/conda env create -q -p /tmp/science -f /conda/science.yml
{
  echo "# SPDX-FileCopyrightText: 2026 Tsolo.io"
  echo "#"
  echo "# SPDX-License-Identifier: Apache-2.0"
  echo "#"
  echo "# Made by deploy/scripts/conda-lock.sh from science.yml with Miniforge $MINIFORGE_VERSION on $(date -u +%F). Do not edit."
  /opt/conda/bin/conda list -p /tmp/science --explicit --md5
} > /conda/science.lock.new
chown --reference=/conda/science.yml /conda/science.lock.new
mv /conda/science.lock.new /conda/science.lock
INSIDE
echo "Wrote $HERE/conda/science.lock ($(grep -c '^https://' "$HERE/conda/science.lock") packages)."
