<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0007 Process config from the environment, runtime settings in the DB

- Status: accepted
- Date: 2026-10-09

## Context
Some settings must be known before the database opens (data root, Docker endpoint, bootstrap admin
password). Others are things an admin changes while the system runs (default limits, GPU vendor,
scratch retention).

## Decision
- Process configuration is a `pydantic-settings` model (`config.Settings`), read from `OW_*`
  environment variables set by `deploy/.env` through compose. Secrets are `SecretStr`.
- Runtime settings live in the DuckDB `settings` table and are edited on `/admin/settings`.
  Environment values such as `OW_GPU_VENDOR` only seed the table on first start.

## Consequences
- Configuration is validated once at startup, with clear errors.
- Changing an environment value after first start does not override a setting an admin changed.