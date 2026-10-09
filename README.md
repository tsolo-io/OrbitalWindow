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
make test             # pytest
make lint             # ruff (make format to fix)
make docs-serve       # zensical docs
make help             # all targets
```

Run the stack locally (Docker required):

```bash
cp deploy/.env.example deploy/.env && chmod 600 deploy/.env   # fill it in
make start            # https://<OW_HOSTNAME>/, log in as admin with CONTROL_ADMIN_PASSWORD
```

Status: phases 1–2 (core, single sign-on, start/stop; desktop images, add-ons and packages) in progress.

- Decisions: [docs/content/adr/](docs/content/adr/index.md)

License: Apache-2.0.
