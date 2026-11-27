<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# 0049 AI usage: read from the LLM gateway when the page opens

- Status: proposed
- Date: 2026-11-27

## Context
The LLM gateway (ADR 0045) records every API request: the key, the model, the endpoint and the
tokens. Admins could see this only in LiteLLM's own admin pages, which are off by default and need
an SSH tunnel. #86 asks for an AI usage report in OrbitalWindow: per day, per user, per model and
per endpoint, and the failed requests.

The free LiteLLM version has what is needed (checked in v1.106.6, the pinned image):
`/user/daily/activity` gives the totals per day with a breakdown by model, endpoint and key
(each key's `user_id` is the OrbitalWindow user name), and `/spend/logs/v2` gives single requests,
filtered by status. `/global/spend/report` is Enterprise only. LiteLLM keeps the single requests
until they are deleted.

## Decision
- **No copy.** The page `/ai/usage` asks the gateway each time it opens or the period changes.
  OrbitalWindow keeps none of these numbers in its own database. The gateway's database `litellm`
  is in the system backups (ADR 0046).
- **Totals from `/user/daily/activity`**, failed requests from `/spend/logs/v2?status_filter=failure`.
  `usage.py` turns the answers into the page's tables and the CSV.
- **The gateway keeps single requests for 90 days** (`maximum_spend_logs_retention_period`,
  set through `/config/field/update` once per gateway start). The totals per day are in another
  table and stay, so only the list of failed requests is limited to 90 days.
- **Who:** admins and AI managers (the AI page). The report shows user names.
- **Tokens, not money.** Local models cost nothing, so the gateway's spend is always 0.

## Consequences
- The chat in Open WebUI goes to the model directly (ADR 0045), so it is not in the report. The
  page says so. Counting it would need Open WebUI to go through the gateway.
- Days are UTC days, as the gateway stores them.
- The report depends on two endpoints that LiteLLM marks as beta. A LiteLLM upgrade must check
  them (`tests/test_gateway.py` has the shapes the code expects).
- When the gateway does not answer, the page shows a short error and nothing else breaks.
