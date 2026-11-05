<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0047 The shared conda: pinned per release, installed by the installer, swapped in

- Status: accepted
- Date: 2026-11-05

## Context
The python-science add-on mounts a shared Miniforge from `OW_ROOT/shared/conda` at `/opt/conda`.
It was installed once by hand (`make install-conda`) with the newest packages of the day. A server
where nobody ran it gave users "Python science (conda) is not installed on the server yet" (#68),
and two servers installed on different days had different versions. Martin (#68): pin the
versions, rebuild only when they change, and let the installer do it.

## Decision
- **Pinned.** `deploy/conda/science.yml` lists the packages; `deploy/scripts/conda-lock.sh` (for
  maintainers) resolves it with the pinned Miniforge (`deploy/conda/miniforge.env`: version and
  published SHA-256) and writes `deploy/conda/science.lock`, an explicit list of every package with
  its URL and MD5. Servers install exactly the lock. Changing a version means a new lock in a
  release.
- **Rebuilt only when changed.** A build writes `.ow-build` (the Miniforge version and a hash of the
  lock). `make install-conda` does nothing when it matches; `FORCE=1` builds anyway.
  `SCIENCE_PACKAGES` still gives an own, unpinned list.
- **Installed by the installer.** `install.sh` runs `make install-conda` on every install and
  update, after the stack is up (`--no-conda` skips it). A failed build is a warning, not an error.
- **Swapped in.** A build goes to `shared/conda.new` (mounted at `/opt/conda` in the builder, as
  conda installs can't be moved), is checked (`import numpy, scipy, pandas, matplotlib, sklearn`)
  and then renamed into place; the current one becomes `shared/conda.old-<swap time>`. A running
  desktop keeps its build, because its bind mount follows the folder, not the name; it gets the
  new one when it starts again. An old build is deleted once no running container with
  `/opt/conda` started before its swap time. A failed build is deleted and the current one stays.

## Consequences
- Every server of one release has the same Python packages; users' notebooks don't change under
  them between releases.
- An update with a new lock downloads about 1-2 GB and needs space for two builds (about 7 GB)
  for a short time.
- Users' own environments (`~/.conda`) are not touched.
- Updating the packages is a maintainer task: edit `science.yml`, run `conda-lock.sh`, test,
  release.
