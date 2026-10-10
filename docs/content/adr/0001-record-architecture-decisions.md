<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0001 Record architecture decisions

- Status: accepted
- Date: 2026-10-09

## Context
The [design](../design.md) was agreed in conversation and a proof of concept. As the system is built,
more decisions are made, and the reasons behind them get lost unless they are written down.

## Decision
Keep Architecture Decision Records in `docs/content/adr/`, one Markdown file per decision, in short
MADR form (context, decision, consequences). The decisions already agreed in the design are recorded
as accepted ADRs. `design.md` stays the description of *what* to build; ADRs explain *why*.

## Consequences
- A reviewer can see why something is the way it is without reading old tickets.
- Changing a decision means adding a superseding ADR, not editing the old one.