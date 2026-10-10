<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0015 Python 3.14, pydantic and httpx2

- Status: accepted
- Date: 2026-10-09

## Context
The project targets current Python. The ticket asked to consider pydantic, pydantic-settings and
httpx2 (pydantic's successor to httpx).

## Decision
- Python 3.14 everywhere: `requires-python`, ruff `target-version = "py314"`, and the service image
  `python:3.14-slim`.
- pydantic-settings for configuration ([0007](0007-configuration.md)), and pydantic models to
  validate structured input such as `addon.yaml`.
- `httpx2` replaces `httpx` for outgoing HTTP calls (for example cAdvisor).

## Consequences
- Python 3.14 syntax and features are allowed (for example `except A, B:` without brackets).