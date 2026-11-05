#!/bin/sh
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0
#
# Install or update the shared Miniforge used by the python-science add-on in $OW_ROOT/shared/conda
# (#68, ADR 0047). It installs exactly deploy/conda/science.lock with the pinned Miniforge
# (conda/miniforge.env), and does nothing when that build is already installed. install.sh runs it
# on every install and update.
#
# A new build is made next to the current one (shared/conda.new) and then swapped in. Desktops
# mount the folder read-only at /opt/conda, so the build is made at that path too (conda installs
# are not relocatable). A running desktop keeps the old build (its bind mount follows the folder,
# not the name) until it restarts; the old build is kept as shared/conda.old-<swap time> and
# removed by a later run once no container that started before that swap is running. Runs in throwaway containers, so the
# host needs only Docker.
#
# Usage: make install-conda   (in deploy/; or OW_ROOT=/srv/orbitalwindow sh scripts/install-conda.sh)
#   FORCE=1                               build again even when the installed build is current
#   SCIENCE_PACKAGES="python=3.13 numpy"  your own package list instead of the lock (not pinned)
set -eu

OW_ROOT=${OW_ROOT:-/srv/orbitalwindow}
HERE=$(cd "$(dirname "$0")/.." && pwd)
SHARED="$OW_ROOT/shared"
LOCK="$HERE/conda/science.lock"
IMAGE=ubuntu:26.04
HELPER=${OW_HELPER_IMAGE:-alpine:3.22}
# shellcheck source-path=SCRIPTDIR source=../conda/miniforge.env
. "$HERE/conda/miniforge.env"

# What makes a build: the Miniforge release and the exact package list.
if [ -n "${SCIENCE_PACKAGES:-}" ]; then
    want="miniforge=$MINIFORGE_VERSION packages=$(printf '%s' "$SCIENCE_PACKAGES" | sha256sum | cut -c1-16)"
    echo "Using SCIENCE_PACKAGES instead of the lock: these packages are not pinned."
else
    want="miniforge=$MINIFORGE_VERSION lock=$(grep -v '^#' "$LOCK" | sha256sum | cut -c1-16)"
fi

# The shared folders belong to root: read and change them through a helper container.
as_root() {  # as_root <shell command>, with $OW_ROOT/shared at /shared
    docker run --rm --network none -v "$SHARED:/shared" "$HELPER" sh -euc "$1"
}

have=$(as_root 'cat /shared/conda/.ow-build 2>/dev/null || true')
if [ "$have" = "$want" ] && [ "${FORCE:-0}" != 1 ]; then
    echo "The shared conda is up to date ($want)."
    exit 0
fi
echo "Building the shared conda ($want); the current one stays in use until it is done."

# Remove old builds that no running container still uses: conda.old-<T> was swapped out at T, so
# only containers with /opt/conda that started before T can use it.
remove_unused_old_builds() {
    for old in $(as_root 'cd /shared && ls -d conda.old-* 2>/dev/null || true'); do
        swapped=${old#conda.old-}
        in_use=""
        for id in $(docker ps -q); do
            started=$(date -d "$(docker inspect -f '{{.State.StartedAt}}' "$id")" +%s 2>/dev/null || echo 0)
            if [ "$started" -lt "$swapped" ] \
                && docker inspect -f '{{range .Mounts}}{{.Destination}} {{end}}' "$id" | grep -qw /opt/conda; then
                in_use="$in_use $(docker inspect -f '{{.Name}}' "$id" | tr -d /)"
            fi
        done
        if [ -n "$in_use" ]; then
            echo "Keeping shared/$old: still used by$in_use (restart them to use the newer build)."
        else
            as_root "rm -rf '/shared/$old'"
            echo "Removed the old build shared/$old."
        fi
    done
}

remove_unused_old_builds
as_root 'rm -rf /shared/conda.new && mkdir -p /shared/conda.new'

build_ok=1
if docker run --rm -i \
    -e MINIFORGE_VERSION="$MINIFORGE_VERSION" -e MINIFORGE_SHA256="$MINIFORGE_SHA256" \
    -e SCIENCE_PACKAGES="${SCIENCE_PACKAGES:-}" -e WANT="$want" \
    -v "$SHARED/conda.new:/opt/conda" \
    -v "$LOCK:/science.lock:ro" \
    "$IMAGE" bash -eu <<'INSIDE'
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq --no-install-recommends curl ca-certificates >/dev/null
curl -fsSL -o /tmp/miniforge.sh "https://github.com/conda-forge/miniforge/releases/download/$MINIFORGE_VERSION/Miniforge3-$MINIFORGE_VERSION-Linux-x86_64.sh"
echo "$MINIFORGE_SHA256  /tmp/miniforge.sh" | sha256sum -c --quiet -
bash /tmp/miniforge.sh -b -u -p /opt/conda >/dev/null
# Users' own environments and package cache go to their home; shared envs stay visible.
cat > /opt/conda/.condarc <<'RC'
envs_dirs:
  - ~/.conda/envs
  - /opt/conda/envs
pkgs_dirs:
  - ~/.conda/pkgs
channels:
  - conda-forge
RC
# Shared envs must be created by path, or conda picks the first envs_dir (a home folder).
if [ -n "$SCIENCE_PACKAGES" ]; then
    # shellcheck disable=SC2086
    CONDA_PKGS_DIRS=/tmp/conda-pkgs /opt/conda/bin/conda create -y -q -p /opt/conda/envs/science \
        $SCIENCE_PACKAGES
else
    CONDA_PKGS_DIRS=/tmp/conda-pkgs /opt/conda/bin/conda create -y -q -p /opt/conda/envs/science \
        --file /science.lock
fi
/opt/conda/envs/science/bin/python -c "import numpy, scipy, pandas, matplotlib, sklearn" \
    || { echo "The new science environment does not work." >&2; exit 1; }
/opt/conda/bin/conda clean -afy -q >/dev/null
rm -rf /tmp/conda-pkgs
printf '%s\n' "$WANT" > /opt/conda/.ow-build
chmod -R a+rX /opt/conda
INSIDE
then :; else build_ok=0; fi
if [ "$build_ok" = 0 ]; then
    as_root 'rm -rf /shared/conda.new'
    echo "The build failed; the current shared conda is unchanged." >&2
    exit 1
fi

# The swap: two renames, so a desktop never sees a half-built folder.
now=$(date +%s)
as_root "[ ! -e /shared/conda ] || mv /shared/conda /shared/conda.old-$now
mv /shared/conda.new /shared/conda"
remove_unused_old_builds
echo "Done: $SHARED/conda ($(as_root 'du -sh /shared/conda | cut -f1')). New desktops use it;" \
    "running ones after a restart. Switch on 'Python science' on the dashboard."
