#!/bin/sh
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0
#
# Make conda available in every shell when /opt/conda is mounted (it is read-only and shared).
set -eu
# Debian, Ubuntu and Arch read /etc/bash.bashrc; Fedora reads /etc/bashrc.
rc=/etc/bash.bashrc
[ -f "$rc" ] || [ ! -f /etc/bashrc ] || rc=/etc/bashrc
cat >> "$rc" <<'HOOK'

# OrbitalWindow python-science add-on
if [ -f /opt/conda/etc/profile.d/conda.sh ]; then
    . /opt/conda/etc/profile.d/conda.sh
    conda activate science 2>/dev/null || true
fi
HOOK
