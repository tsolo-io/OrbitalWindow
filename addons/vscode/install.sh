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
# Chromium's sandbox can't create its namespaces in a desktop container, so VS Code aborts at
# start unless it runs with --no-sandbox (ADR 0022): in the menu entries and on the command line.
sed -i 's|^Exec=/usr/share/code/code|Exec=/usr/share/code/code --no-sandbox|' \
    /usr/share/applications/com.microsoft.VSCode*.desktop
printf '#!/bin/sh\nexec /usr/bin/code --no-sandbox "$@"\n' > /usr/local/bin/code
chmod 755 /usr/local/bin/code
