# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Benchmarks for every user: all results, and the full report of one benchmark."""

from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import run, ui

from ..benchmarks import task_results
from .common import authenticate, frame, guarded, local_time, service

COLUMNS = [
    {"name": "repo", "label": "Model", "field": "repo", "align": "left", "sortable": True},
    {"name": "suite", "label": "Benchmark", "field": "suite", "align": "left", "sortable": True},
    {"name": "plus", "label": "Score (plus)", "field": "plus", "align": "right", "sortable": True},
    {"name": "base", "label": "Score (base)", "field": "base", "align": "right", "sortable": True},
    {"name": "status", "label": "Status", "field": "status", "align": "left"},
    {"name": "when", "label": "Run", "field": "when", "align": "left", "sortable": True},
    {"name": "duration", "label": "Took", "field": "duration", "align": "right"},
    {"name": "settings", "label": "Settings", "field": "settings", "align": "left"},
    {"name": "report", "label": "", "field": "report", "align": "right"},
]
EXPLANATION = (
    "Coding benchmarks from EvalPlus: the model writes a Python function for each task, and the "
    "function is run against tests. Base uses the original tests (HumanEval, MBPP); plus adds "
    "many more tests (HumanEval+, MBPP+) and is the stricter, more reliable score. One greedy "
    "answer per task, thinking switched off. Repeated runs can differ by a few points, because "
    "the server answers many tasks at once."
)


def percent(value: float | None) -> str:
    """Format a score.

    Args:
        value: Share from 0 to 1, or None.

    Returns:
        For example ``36.6 %``; ``–`` for None.
    """
    return f"{value * 100:.1f} %" if value is not None else "–"


def duration(seconds: float | None) -> str:
    """Format a run time.

    Args:
        seconds: Seconds, or None.

    Returns:
        For example ``3 min 12 s``; ``–`` for None.
    """
    if seconds is None:
        return "–"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes} min {rest} s" if minutes else f"{rest} s"


def settings_text(row: dict[str, Any]) -> str:
    """Describe the session settings a benchmark ran with.

    Args:
        row: The benchmark.

    Returns:
        For example ``GPU 0 · 8,192 tokens · 35 %``.
    """
    if row["gpu_index"] is None:
        return "–"
    return f"GPU {row['gpu_index']} · {row['context']:,} tokens · {row['gpu_share']:.0%}"


@ui.page("/benchmarks")
def benchmarks_page(request: Request) -> RedirectResponse | None:
    """Render the list of all benchmarks.

    Args:
        request: The incoming page request.

    Returns:
        A redirect if not logged in, else None (the page is rendered).
    """
    auth = authenticate(request)
    if isinstance(auth, RedirectResponse):
        return auth
    svc = service()
    frame(auth, "Benchmarks")
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        ui.label("AI model benchmarks").classes("text-h5")
        ui.label(EXPLANATION).classes("text-grey-8")
        table = ui.table(columns=COLUMNS, rows=[], row_key="id").classes("w-full")
        table.add_slot(
            "body-cell-report",
            """
            <q-td :props="props">
              <a v-if="props.row.status === 'done'" :href="'/benchmarks/' + props.row.id">
                Report</a>
            </q-td>
            """,
        )

        async def refresh() -> None:
            """Reload the table."""
            rows = await run.io_bound(svc.list_benchmarks)
            if rows is None:  # the page closed or the app is stopping
                return
            table.rows = [
                {
                    "id": row["id"],
                    "repo": row["repo"],
                    "suite": row["suite_label"],
                    "plus": percent(row["plus_score"]),
                    "base": percent(row["base_score"]),
                    "status": row["status"],
                    "when": local_time(row["created_at"]),
                    "duration": duration(row["seconds"]),
                    "settings": settings_text(row),
                }
                for row in rows
            ]
            table.update()

        ui.timer(10.0, guarded(auth, refresh))
        ui.timer(0.1, guarded(auth, refresh), once=True)
    return None


@ui.page("/benchmarks/{bench_id}")
def benchmark_report(request: Request, bench_id: int) -> RedirectResponse | None:
    """Render the full report of one benchmark.

    Args:
        request: The incoming page request.
        bench_id: The benchmark.

    Returns:
        A redirect if not logged in, else None (the page is rendered).
    """
    auth = authenticate(request)
    if isinstance(auth, RedirectResponse):
        return auth
    svc = service()
    frame(auth, "Benchmark report")
    row = svc.get_benchmark(bench_id)
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        ui.link("← All benchmarks", "/benchmarks")
        if row is None:
            ui.label("There is no such benchmark.").classes("text-h6")
            return None
        ui.label(f"{row['suite_label']}: {row['repo']}").classes("text-h5")
        with ui.card().classes("w-full"):
            with ui.row().classes("gap-12"):
                for label, value in (
                    ("Score (plus)", percent(row["plus_score"])),
                    ("Score (base)", percent(row["base_score"])),
                    (
                        "Tasks passed (plus / base / all)",
                        f"{row['plus_pass']} / {row['base_pass']} / {row['tasks']}"
                        if row["tasks"]
                        else "–",
                    ),
                ):
                    with ui.column().classes("gap-0"):
                        ui.label(label).classes("text-caption text-grey-7")
                        ui.label(value).classes("text-h6")
            ui.label(
                f"Status {row['status']} · run {local_time(row['created_at'])} by "
                f"{row['started_by']} · took {duration(row['seconds'])} · {settings_text(row)}"
            ).classes("text-grey-8")
            if row["error"]:
                ui.code(row["error"]).classes("w-full text-xs")
            ui.label(EXPLANATION).classes("text-caption text-grey-7")
            with ui.row().classes("gap-4"):
                for name in ("samples.jsonl", "raw.jsonl", "samples_eval_results.json"):
                    ui.link(name, f"/benchmarks/{bench_id}/files/{name}").classes("text-sm")
        tasks = task_results(svc.benchmarks.folder(bench_id))
        if tasks:
            ui.label("Tasks").classes("text-h6")
            columns = [
                {"name": "task_id", "label": "Task", "field": "task_id", "align": "left"},
                {"name": "base", "label": "Base tests", "field": "base", "align": "left"},
                {"name": "plus", "label": "Plus tests", "field": "plus", "align": "left"},
            ]
            table = ui.table(columns=columns, rows=tasks, row_key="task_id", pagination=50).classes(
                "w-full"
            )
            table.add_slot(
                "body",
                """
                <q-tr :props="props" @click="props.expand = !props.expand" class="cursor-pointer">
                  <q-td key="task_id" :props="props">{{ props.row.task_id }}</q-td>
                  <q-td key="base" :props="props">
                    <q-badge :color="props.row.base === 'pass' ? 'positive' : 'negative'"
                             :label="props.row.base" /></q-td>
                  <q-td key="plus" :props="props">
                    <q-badge :color="props.row.plus === 'pass' ? 'positive' : 'negative'"
                             :label="props.row.plus" /></q-td>
                </q-tr>
                <q-tr v-show="props.expand" :props="props">
                  <q-td colspan="100%">
                    <pre style="white-space: pre-wrap; font-size: 12px"
                    >{{ props.row.solution }}</pre>
                  </q-td>
                </q-tr>
                """,
            )
            ui.label("Click a task to see the code the model wrote.").classes(
                "text-caption text-grey-7"
            )
    return None
