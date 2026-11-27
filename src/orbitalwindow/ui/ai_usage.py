# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""AI managers: the AI usage report (#86), read from the LLM gateway each time."""

from datetime import date, datetime
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import run, ui

from .. import usage
from .common import authenticate, frame, guarded, local_time, service

NOTE = (
    "Use of the AI by programs and editors with a personal API key (sk-ow-…), counted by the "
    "LLM gateway. The chat goes to the model directly, so it is not counted here. Days in the "
    "chart and tables are UTC days. The list of failed requests goes back at most 90 days: "
    "the gateway deletes older requests, but keeps the totals per day."
)
TOKEN_HELP = "Tokens in: the prompt. Tokens out: the answer. From cache: prompt tokens re-used."


def columns(first: str) -> list[dict[str, Any]]:
    """Return the columns of a breakdown table.

    Args:
        first: Label of the first column, e.g. "User".

    Returns:
        NiceGUI table columns.
    """
    numbers = [
        ("requests", "Requests"),
        ("failed", "Failed"),
        ("tokens_in", "Tokens in"),
        ("tokens_out", "Tokens out"),
        ("tokens_cached", "From cache"),
        ("share", "Share of tokens"),
    ]
    name = {"name": "name", "label": first, "field": "name", "align": "left", "sortable": True}
    return [name] + [
        {"name": key, "label": label, "field": key, "align": "right", "sortable": key != "share"}
        for key, label in numbers
    ]


FAILED_COLUMNS = [
    {"name": "time", "label": "Time", "field": "time", "align": "left"},
    {"name": "user", "label": "User", "field": "user", "align": "left"},
    {"name": "model", "label": "Model", "field": "model", "align": "left"},
    {"name": "endpoint", "label": "Endpoint", "field": "endpoint", "align": "left"},
    {"name": "error", "label": "Error", "field": "error", "align": "left"},
]


def chart_options(days: list[tuple[date, usage.Totals]]) -> dict[str, Any]:
    """Return the per-day chart: requests as bars, tokens as a line.

    Args:
        days: The report's history.

    Returns:
        ECharts options.
    """
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["Requests", "Tokens"]},
        "grid": {"left": 60, "right": 70, "top": 40, "bottom": 30},
        "xAxis": {"type": "category", "data": [usage.day_label(day) for day, _ in days]},
        "yAxis": [
            {"type": "value", "name": "Requests", "minInterval": 1},
            {"type": "value", "name": "Tokens"},
        ],
        "series": [
            {"name": "Requests", "type": "bar", "data": [t.requests for _, t in days]},
            {
                "name": "Tokens",
                "type": "line",
                "yAxisIndex": 1,
                "data": [t.tokens for _, t in days],
            },
        ],
    }


def shown_time(value: str) -> str:
    """Show a time from the gateway's request log in the server's time zone.

    Args:
        value: ISO time in UTC, as the gateway gives it.

    Returns:
        The local time, or the value as it is when it is not an ISO time.
    """
    try:
        return local_time(datetime.fromisoformat(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


@ui.page("/ai/usage")
def ai_usage_page(request: Request) -> RedirectResponse | None:
    """Render the AI usage report for AI managers and admins.

    Args:
        request: The incoming page request.

    Returns:
        A redirect if not logged in as an AI manager or admin, else None (the page is rendered).
    """
    auth = authenticate(request, ai=True)
    if isinstance(auth, RedirectResponse):
        return auth
    svc = service()
    frame(auth, "AI usage")
    state: dict[str, Any] = {"csv": ""}
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center"):
            ui.label("AI usage").classes("text-h5")
            ui.space()
            ui.link("Back to AI", "/ai")
        ui.label(NOTE).classes("text-grey-8")
        error = ui.label().classes("text-negative")
        error.set_visibility(False)

        with ui.card().classes("w-full"):
            ui.label(f"Last {usage.HISTORY_DAYS} days").classes("text-h6")
            chart = ui.echart(chart_options([])).classes("w-full h-64")
            totals = ui.label().classes("text-body2")

        with ui.row().classes("w-full items-center gap-4"):
            period = ui.toggle(list(usage.PERIODS), value="7 days").props("no-caps")
            ui.label(TOKEN_HELP).classes("text-caption text-grey-7")
            ui.space()
            ui.button(
                "Download CSV",
                icon="download",
                on_click=lambda: ui.download.content(state["csv"], "ai-usage.csv", "text/csv"),
            ).props("no-caps outline")

        tables = {}
        for key, title, first in (
            ("user", "Per user", "User"),
            ("model", "Per model", "Model"),
            ("endpoint", "Per endpoint", "Endpoint"),
        ):
            ui.label(title).classes("text-h6")
            tables[key] = ui.table(columns=columns(first), rows=[], row_key="name").classes(
                "w-full"
            )
        ui.label("Failed requests").classes("text-h6")
        failed = ui.table(columns=FAILED_COLUMNS, rows=[], row_key="i", pagination=20).classes(
            "w-full"
        )

        async def refresh() -> None:
            """Read the report from the gateway and show it."""
            days = usage.PERIODS[period.value]
            result = await run.io_bound(svc.ai_usage, days)
            if result is None:  # the page closed or the app is stopping
                return
            report, failures, problem = result
            error.set_text(problem)
            error.set_visibility(bool(problem))
            if report is None:
                return
            chart.options.clear()
            chart.options.update(chart_options(report.days))
            chart.update()
            history = usage.Totals()
            for _, day in report.days:
                history.requests += day.requests
                history.failed += day.failed
                history.tokens_in += day.tokens_in
                history.tokens_out += day.tokens_out
            totals.set_text(
                f"{history.requests:,} requests ({history.failed:,} failed) · "
                f"{history.tokens_in:,} tokens in · {history.tokens_out:,} tokens out"
            )
            tables["user"].rows = usage.table_rows(report.by_user, report.total)
            tables["model"].rows = usage.table_rows(report.by_model, report.total)
            tables["endpoint"].rows = usage.table_rows(report.by_endpoint, report.total)
            failed.rows = [
                row | {"i": index, "time": shown_time(row["time"])}
                for index, row in enumerate(failures)
            ]
            for table in [*tables.values(), failed]:
                table.update()
            state["csv"] = usage.to_csv(report)

        period.on_value_change(guarded(auth, refresh))
        ui.timer(0.1, guarded(auth, refresh), once=True)
    return None
