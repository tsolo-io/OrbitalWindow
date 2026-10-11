#!/bin/sh
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0
#
# Install the shared Miniforge used by the python-science add-on into $OW_ROOT/shared/conda.
# Desktops mount it read-only at /opt/conda, so it is installed at that same path (conda
# installs are not relocatable). Runs in a throwaway container: the host needs only Docker.
#
# Usage: make install-conda   (in deploy/; or OW_ROOT=/srv/orbitalwindow sh scripts/install-conda.sh)
#   SCIENCE_PACKAGES="python=3.13 numpy"  override the packages of the "science" environment
#   FORCE=1                               reinstall over an existing install
set -eu

OW_ROOT=${OW_ROOT:-/srv/orbitalwindow}
TARGET="$OW_ROOT/shared/conda"
SCIENCE_PACKAGES=${SCIENCE_PACKAGES:-"python=3.13 numpy scipy pandas matplotlib scikit-learn \
jupyterlab spyder nb_conda_kernels ipykernel"}
IMAGE=ubuntu:26.04

if [ -e "$TARGET/bin/conda" ] && [ "${FORCE:-0}" != 1 ]; then
    echo "Miniforge is already installed in $TARGET (set FORCE=1 to reinstall)." >&2
    exit 1
fi
# docker creates $TARGET for the bind mount, so this works without sudo for the docker group.

docker run --rm -i \
    -e SCIENCE_PACKAGES="$SCIENCE_PACKAGES" \
    -v "$TARGET:/opt/conda" \
    "$IMAGE" bash -eu <<'INSIDE'
apt-get update -qq && apt-get install -y -qq --no-install-recommends curl ca-certificates >/dev/null
curl -fsSL -o /tmp/miniforge.sh \
    https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
bash /tmp/miniforge.sh -b -u -p /opt/conda
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
CONDA_PKGS_DIRS=/tmp/conda-pkgs /opt/conda/bin/conda create -y -q -p /opt/conda/envs/science \
    $SCIENCE_PACKAGES
/opt/conda/bin/conda clean -afy -q >/dev/null
rm -rf /tmp/conda-pkgs
chmod -R a+rX /opt/conda
INSIDE

echo "Done: $TARGET ($(du -sh "$TARGET" | cut -f1)). Switch on 'Python science' in the dashboard."
