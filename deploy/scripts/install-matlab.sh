#!/bin/sh
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0
#
# Install the shared MATLAB used by the matlab add-on into $OW_ROOT/shared/matlab, with
# MathWorks Package Manager (mpm). No licence is needed to install: each user activates their
# own licence in their desktop the first time they start MATLAB. Runs in a throwaway container.
#
# Usage: make install-matlab [PRODUCTS="…"]   (in deploy/; or OW_ROOT=… sh scripts/install-matlab.sh [product ...])
#   default products: MATLAB Parallel_Computing_Toolbox   (about 8 GB)
#   RELEASE=R2026b                                        MATLAB release
set -eu

OW_ROOT=${OW_ROOT:-/srv/orbitalwindow}
TARGET="$OW_ROOT/shared/matlab"
RELEASE=${RELEASE:-R2026b}
IMAGE=ubuntu:26.04
if [ "$#" -eq 0 ]; then
    set -- MATLAB Parallel_Computing_Toolbox
fi

if [ -e "$TARGET/bin/matlab" ]; then
    echo "MATLAB is already installed in $TARGET. Remove it first to reinstall." >&2
    exit 1
fi
# docker creates $TARGET for the bind mount, so this works without sudo for the docker group.

docker run --rm -e DEBIAN_FRONTEND=noninteractive \
    -v "$TARGET:/opt/matlab" \
    "$IMAGE" bash -euc '
# mpm unpacks a binary that needs libatomic1, which is not in the base image.
apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    curl ca-certificates unzip libatomic1 >/dev/null
curl -fsSL -o /tmp/mpm https://www.mathworks.com/mpm/glnxa64/mpm
chmod +x /tmp/mpm
/tmp/mpm install --release="$0" --destination=/opt/matlab --products "$@"
chmod -R a+rX /opt/matlab
' "$RELEASE" "$@"

echo "Done: $TARGET ($(du -sh "$TARGET" | cut -f1))."
echo "Grant the MATLAB add-on to users on the Users page (Software access)."
