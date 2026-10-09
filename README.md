<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# OrbitalWindow

Manage GPU-accelerated research/science desktops on Docker. Each user gets their own Linux desktop
(LinuxServer webtop) in the browser. OrbitalWindow is the single login where they start, stop and open it,
pick software, and reach their files. Admins manage users, desktop images, add-ons and groups.

Built with Python, FastAPI + NiceGUI and DuckDB.

- Design: [docs/content/design.md](docs/content/design.md)
- Proof-of-concept learnings: [docs/content/poc-learnings.md](docs/content/poc-learnings.md)

## Development

```bash
hatch test            # pytest
hatch run lint:check  # ruff (lint:fmt to fix)
hatch run docs:serve  # zensical docs
```

Status: design stage, nothing implemented yet.

License: AGPL-3.0.
