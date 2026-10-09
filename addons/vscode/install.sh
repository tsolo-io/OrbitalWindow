#!/bin/sh
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0
set -eu
curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
  | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/code stable main" \
  > /etc/apt/sources.list.d/vscode.list
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends code
# Electron's sandbox helper must be setuid root, or VS Code aborts at start.
chown root:root /usr/share/code/chrome-sandbox
chmod 4755 /usr/share/code/chrome-sandbox
