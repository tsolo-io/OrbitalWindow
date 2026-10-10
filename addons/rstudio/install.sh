#!/bin/sh
# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0
#
# Install the current RStudio Desktop .deb from Posit. Posit's downloads.json lists an installer
# per Ubuntu codename; use ours, else the newest Ubuntu build listed.
set -eu
. /etc/os-release
url=$(curl -fsSL https://cdn.posit.co/downloads.json | python3 -c '
import json, sys
codename = sys.argv[1]
inst = json.load(sys.stdin)["rstudio"]["open_source"]["stable"]["desktop"]["installer"]
for name in (codename, "resolute", "noble", "jammy"):
    if name in inst and inst[name]["url"].endswith(".deb"):
        print(inst[name]["url"]); break
else:
    sys.exit("no RStudio .deb for Ubuntu found in downloads.json")
' "$VERSION_CODENAME")
echo "RStudio: $url"
curl -fsSL -o /tmp/rstudio.deb "$url"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends /tmp/rstudio.deb
rm -f /tmp/rstudio.deb
# Electron's sandbox helper must be setuid root.
if [ -f /usr/lib/rstudio/chrome-sandbox ]; then
    chown root:root /usr/lib/rstudio/chrome-sandbox
    chmod 4755 /usr/lib/rstudio/chrome-sandbox
fi
