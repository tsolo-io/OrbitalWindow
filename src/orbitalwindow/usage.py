# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The AI usage report (#86): the LLM gateway's numbers, grouped for the AI page.

The gateway (ADR 0045) counts every API request per day, with a breakdown by model, endpoint and
key. Each key's ``user_id`` is the OrbitalWindow user name. Nothing is copied into OrbitalWindow's
database: the report is built from the gateway's answers each time the page asks. The chat in
Open WebUI goes to the model directly, so it is not in these numbers.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

# Periods the page offers: label and number of days, today included.
PERIODS = {"Today": 1, "7 days": 7, "30 days": 30}
HISTORY_DAYS = 30
# Endpoint names as users know them; the gateway reports the API path.
ENDPOINT_NAMES = (
    ("chat/completions", "Chat"),
    ("completions", "Code completion"),
    ("embeddings", "Embeddings"),
)
# The request log names the endpoint by LiteLLM's call type.
CALL_TYPES = {
    "acompletion": "Chat",
    "completion": "Chat",
    "atext_completion": "Code completion",
    "text_completion": "Code completion",
    "aembedding": "Embeddings",
    "embedding": "Embeddings",
}
UNKNOWN_ENDPOINT = "Not known"


@dataclass
class Totals:
    """Requests and tokens, summed."""

    requests: int = 0
    failed: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0

    def add(self, metrics: dict[str, Any]) -> None:
        """Add one of the gateway's ``metrics`` blocks.

        Args:
            metrics: LiteLLM ``SpendMetrics`` as a dict.
        """
        self.requests += int(metrics.get("api_requests") or 0)
        self.failed += int(metrics.get("failed_requests") or 0)
        self.tokens_in += int(metrics.get("prompt_tokens") or 0)
        self.tokens_out += int(metrics.get("completion_tokens") or 0)
        self.tokens_cached += int(metrics.get("cache_read_input_tokens") or 0)

    @property
    def tokens(self) -> int:
        """Return the tokens in and out together.

        Returns:
            The sum.
        """
        return self.tokens_in + self.tokens_out


@dataclass
class Report:
    """The usage report for one period, and the per-day history."""

    period_days: int
    days: list[tuple[date, Totals]] = field(default_factory=list)
    total: Totals = field(default_factory=Totals)
    by_user: dict[str, Totals] = field(default_factory=dict)
    by_model: dict[str, Totals] = field(default_factory=dict)
    by_endpoint: dict[str, Totals] = field(default_factory=dict)


def endpoint_name(path: str) -> str:
    """Return the name users know for an API endpoint.

    Args:
        path: The endpoint as the gateway reports it, e.g. ``/v1/chat/completions``.

    Returns:
        For example "Chat" or "Code completion"; other paths as they are.
    """
    for suffix, name in ENDPOINT_NAMES:
        if path.rstrip("/").endswith(suffix):
            return name
    return path or "other"


def key_user(key: dict[str, Any]) -> str:
    """Return the OrbitalWindow user of a key in the gateway's breakdown.

    Args:
        key: One entry of ``breakdown.api_keys``.

    Returns:
        The user name (the key's ``user_id``, else its ``ow-<user>`` alias), or "unknown".
    """
    meta = key.get("metadata") or {}
    if meta.get("user_id"):
        return str(meta["user_id"])
    alias = str(meta.get("key_alias") or "")
    return alias.removeprefix("ow-") if alias.startswith("ow-") else "unknown"


def build_report(days: list[dict[str, Any]], today: date, period_days: int) -> Report:
    """Build the report from the gateway's per-day rows.

    Args:
        days: Rows of ``Gateway.daily_activity`` for at least the last ``HISTORY_DAYS`` days.
        today: The last day of the report (UTC, as the gateway counts days).
        period_days: Days in the period for the per-user, per-model and per-endpoint tables.

    Returns:
        The report. The history has every day of the last ``HISTORY_DAYS``, also days without use.
    """
    report = Report(period_days=period_days)
    by_day = {str(row.get("date"))[:10]: row for row in days}
    period_start = today - timedelta(days=period_days - 1)
    for offset in range(HISTORY_DAYS - 1, -1, -1):
        day = today - timedelta(days=offset)
        totals = Totals()
        row = by_day.get(day.isoformat())
        if row is not None:
            totals.add(row.get("metrics") or {})
        report.days.append((day, totals))
        if row is None or day < period_start:
            continue
        report.total.add(row.get("metrics") or {})
        breakdown = row.get("breakdown") or {}
        for key in (breakdown.get("api_keys") or {}).values():
            report.by_user.setdefault(key_user(key), Totals()).add(key.get("metrics") or {})
        # Model groups are the names clients use (the session's model); "models" has
        # LiteLLM's provider prefix, e.g. "openai/…".
        models = breakdown.get("model_groups") or breakdown.get("models") or {}
        for model, entry in models.items():
            report.by_model.setdefault(model, Totals()).add(entry.get("metrics") or {})
        for path, entry in (breakdown.get("endpoints") or {}).items():
            name = endpoint_name(path)
            report.by_endpoint.setdefault(name, Totals()).add(entry.get("metrics") or {})
    _add_unknown_endpoint(report)
    return report


def _add_unknown_endpoint(report: Report) -> None:
    """Add a row for requests the gateway counted without an endpoint.

    Some failed requests (for example a model that can't be reached) are in the day's totals,
    but in no endpoint. The row keeps the endpoint table's sum equal to the total.

    Args:
        report: The report, changed in place.
    """
    rest = Totals()
    for name in ("requests", "failed", "tokens_in", "tokens_out", "tokens_cached"):
        counted = sum(getattr(totals, name) for totals in report.by_endpoint.values())
        setattr(rest, name, max(0, getattr(report.total, name) - counted))
    if rest.requests or rest.tokens:
        report.by_endpoint[UNKNOWN_ENDPOINT] = rest


def day_label(day: date) -> str:
    """Return a short label for a day in the chart.

    Args:
        day: The day (a UTC day, as the gateway counts them).

    Returns:
        For example "27 Nov".
    """
    return f"{day.day} {day:%b}"


def share(part: int, whole: int) -> str:
    """Return a part of a total as a percentage for a table.

    Args:
        part: The part.
        whole: The total.

    Returns:
        For example "42 %", or "–" when the total is 0.
    """
    return f"{round(100 * part / whole)} %" if whole else "–"


def table_rows(groups: dict[str, Totals], total: Totals) -> list[dict[str, Any]]:
    """Return table rows for a breakdown, most tokens first.

    Args:
        groups: Totals by name (user, model or endpoint).
        total: The period's total, for the share.

    Returns:
        Rows with ``name``, ``requests``, ``failed``, ``tokens_in``, ``tokens_out``,
        ``tokens_cached`` and ``share`` (of the tokens).
    """
    ordered = sorted(groups.items(), key=lambda item: (-item[1].tokens, -item[1].requests, item[0]))
    return [
        {
            "name": name,
            "requests": totals.requests,
            "failed": totals.failed,
            "tokens_in": totals.tokens_in,
            "tokens_out": totals.tokens_out,
            "tokens_cached": totals.tokens_cached,
            "share": share(totals.tokens, total.tokens),
        }
        for name, totals in ordered
    ]


def failed_rows(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return table rows for failed requests.

    Args:
        logs: Rows of ``Gateway.failed_requests``.

    Returns:
        Rows with ``time`` (as the gateway gives it, UTC), ``user``, ``model``, ``endpoint``
        and ``error``.
    """
    rows = []
    for log in logs:
        meta = log.get("metadata") or {}
        error = meta.get("error_information") or {}
        code = str(error.get("error_code") or "")
        message = str(error.get("error_message") or "")[:200]
        rows.append(
            {
                "time": str(log.get("startTime") or ""),
                "user": str(log.get("user") or meta.get("user_api_key_user_id") or "unknown"),
                "model": str(log.get("model") or ""),
                "endpoint": CALL_TYPES.get(str(log.get("call_type") or ""), UNKNOWN_ENDPOINT),
                "error": f"{code} {message}".strip(),
            }
        )
    return rows


def to_csv(report: Report) -> str:
    """Return the report's tables as one CSV text.

    Args:
        report: The report.

    Returns:
        CSV with a ``table`` column (day, user, model or endpoint) and the numbers.
    """
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["table", "name", "requests", "failed", "tokens_in", "tokens_out",
                     "tokens_cached"])  # fmt: skip
    for day, totals in report.days:
        writer.writerow(["day", day.isoformat(), totals.requests, totals.failed, totals.tokens_in,
                         totals.tokens_out, totals.tokens_cached])  # fmt: skip
    for table, groups in (("user", report.by_user), ("model", report.by_model),
                          ("endpoint", report.by_endpoint)):  # fmt: skip
        for row in table_rows(groups, report.total):
            writer.writerow([table, row["name"], row["requests"], row["failed"], row["tokens_in"],
                             row["tokens_out"], row["tokens_cached"]])  # fmt: skip
    return out.getvalue()
