# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Admin: system. Every desktop with its state and CPU/RAM use, the GPUs, links to cAdvisor,
and the audit log."""

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import run, ui

from ..metrics import fmt_bytes
from .common import authenticate, frame, guarded, local_time, service

DESKTOP_COLUMNS = [
    {"name": "name", "label": "User", "field": "name", "sortable": True, "align": "left"},
    {"name": "state", "label": "Desktop", "field": "state", "sortable": True, "align": "left"},
    {"name": "image", "label": "Image", "field": "image", "align": "left"},
    {"name": "cpu", "label": "CPU use", "field": "cpu", "align": "right"},
    {"name": "cpu_limit", "label": "CPU limit", "field": "cpu_limit", "align": "right"},
    {"name": "mem", "label": "RAM use", "field": "mem", "align": "right"},
    {"name": "mem_limit", "label": "RAM limit", "field": "mem_limit", "align": "right"},
]
DISK_COLUMNS = [
    {"name": "subject", "label": "Kind", "field": "subject", "align": "left"},
    {"name": "name", "label": "Name", "field": "name", "align": "left"},
    {"name": "used", "label": "Used", "field": "used", "align": "right"},
    {"name": "quota", "label": "Quota", "field": "quota", "align": "right"},
    {"name": "percent", "label": "Of quota", "field": "percent", "align": "right"},
    {"name": "measured", "label": "Measured", "field": "measured", "align": "right"},
]
AUDIT_COLUMNS = [
    {"name": "ts", "label": "Time", "field": "ts", "align": "left"},
    {"name": "actor", "label": "Who", "field": "actor", "align": "left"},
    {"name": "action", "label": "Action", "field": "action", "align": "left"},
    {"name": "target", "label": "Target", "field": "target", "align": "left"},
    {"name": "detail", "label": "Detail", "field": "detail", "align": "left"},
]


@ui.page("/admin/system")
def admin_system(request: Request) -> RedirectResponse | None:
    """Render the admin system page.

    Args:
        request: The incoming page request.

    Returns:
        A redirect if not logged in as an admin, else None (the page is rendered).
    """
    auth = authenticate(request, admin=True)
    if isinstance(auth, RedirectResponse):
        return auth
    svc = service()
    frame(auth, "System")
    with ui.column().classes("w-full max-w-7xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("System").classes("text-h5")
            with ui.row().classes("gap-2"):
                ui.button(
                    "Logs",
                    icon="article",
                    on_click=lambda: ui.navigate.to("/admin/logs/", new_tab=True),
                ).props("no-caps outline")
                ui.button(
                    "cAdvisor",
                    icon="insights",
                    on_click=lambda: ui.navigate.to("/admin/cadvisor/", new_tab=True),
                ).props("no-caps outline")
        cadvisor_note = ui.label(
            "Container metrics are not available: cAdvisor is not reachable."
        ).classes("text-warning")

        with ui.card().classes("w-full"):
            ui.label("Desktops").classes("text-h6")
            desktops = ui.table(columns=DESKTOP_COLUMNS, rows=[], row_key="name").classes("w-full")

        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Disk usage").classes("text-h6")
                measure_btn = ui.button("Measure now", icon="refresh").props("no-caps flat")
            host_label = ui.label("")
            disk = ui.table(columns=DISK_COLUMNS, rows=[], row_key="key").classes("w-full")
            disk.props("dense")
            disk.add_slot(
                "body-cell-percent",
                """<q-td :props="props"><span :class="props.row.warning ? 'text-negative' : ''">
                   {{ props.row.percent }}</span></q-td>""",
            )

        async def refresh_disk() -> None:
            """Reload the server's free space and the disk usage table."""
            usage, host = await run.io_bound(svc.disk_overview)
            free = fmt_bytes(host.free)
            text = f"Free on the server: {free} of {fmt_bytes(host.total)}"
            if host.warning:
                text += f" — the disk is {host.used_fraction:.0%} full. Free up space soon."
            host_label.set_text(text)
            host_label.classes(replace="text-negative" if host.warning else "text-grey-8")
            disk.rows = [
                {
                    "key": f"{entry.subject}:{entry.name}",
                    "subject": entry.subject,
                    "name": entry.name,
                    "used": fmt_bytes(entry.bytes),
                    "quota": f"{entry.quota_gb:g} GB" if entry.quota_gb else "–",
                    "percent": f"{entry.fraction:.0%}" if entry.fraction is not None else "–",
                    "warning": entry.warning,
                    "measured": local_time(entry.measured_at),
                }
                for entry in usage
            ]
            disk.update()

        async def on_measure() -> None:
            """Measure disk usage now, then reload the table."""
            measure_btn.disable()
            try:
                await run.io_bound(svc.measure_disk)
            finally:
                measure_btn.enable()
            await refresh_disk()
            ui.notify("Disk usage measured.")

        measure_btn.on_click(guarded(auth, on_measure))
        ui.timer(0.1, guarded(auth, refresh_disk), once=True)
        ui.timer(60.0, guarded(auth, refresh_disk))

        with ui.card().classes("w-full"):
            ui.label("GPUs").classes("text-h6")
            gpu_box = ui.column().classes("w-full gap-2")

        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Audit log").classes("text-h6")
                search = ui.input("Search", placeholder="user, action…").props(
                    "dense outlined clearable"
                )
            audit = ui.table(columns=AUDIT_COLUMNS, rows=[], row_key="i").classes("w-full")
            audit.props("dense :pagination='{rowsPerPage: 25}'")

        async def refresh() -> None:
            """Reload the desktop table and the GPU statistics."""
            rows = await run.io_bound(svc.system_overview)
            cadvisor_note.set_visibility(not svc.metrics.available)
            desktops.rows = [
                {
                    "name": row["name"] + (" (disabled)" if row["disabled"] else ""),
                    "state": row["state"],
                    "image": row["image_label"] or "–",
                    "cpu": f"{row['cpu_cores']:.2f} cores" if row["cpu_cores"] is not None else "–",
                    "cpu_limit": f"{row['cpus']:g}" if row["cpus"] else "unlimited",
                    "mem": fmt_bytes(row["mem_bytes"]) if row["mem_bytes"] is not None else "–",
                    "mem_limit": f"{row['mem_gb']:g} GB" if row["mem_gb"] else "unlimited",
                }
                for row in rows
            ]
            desktops.update()
            gpus = await run.io_bound(svc.gpus.stats)
            gpu_box.clear()
            with gpu_box:
                if not gpus:
                    ui.label("No GPU statistics (no NVIDIA GPU, or NVML is not available).")\
                        .classes("text-grey-7")  # fmt: skip
                for gpu in gpus:
                    gpu_line(gpu)

        async def refresh_audit() -> None:
            """Reload the audit log table, filtered by the search text."""
            rows = await run.io_bound(svc.audit_log, search.value or "", 500)
            audit.rows = [
                row | {"i": index, "ts": local_time(row["ts"], "%Y-%m-%d %H:%M:%S")}
                for index, row in enumerate(rows)
            ]
            audit.update()

        search.on_value_change(guarded(auth, refresh_audit))
        ui.timer(5.0, guarded(auth, refresh))
        ui.timer(0.1, guarded(auth, refresh), once=True)
        ui.timer(0.1, guarded(auth, refresh_audit), once=True)
        ui.timer(30.0, guarded(auth, refresh_audit))
    return None


def gpu_line(gpu) -> None:
    """Add a GPU's name with bars for its load and memory use.

    Args:
        gpu: GPU statistics (index, name, utilisation and memory).
    """
    with ui.column().classes("w-full gap-1"):
        ui.label(f"GPU {gpu.index}: {gpu.name}").classes("font-medium")
        with ui.row().classes("w-full items-center no-wrap"):
            ui.label("Load").classes("w-16 text-sm")
            ui.linear_progress(gpu.util_percent / 100, show_value=False).classes("grow")
            ui.label(f"{gpu.util_percent}%").classes("w-32 text-sm text-right")
        with ui.row().classes("w-full items-center no-wrap"):
            ui.label("Memory").classes("w-16 text-sm")
            ui.linear_progress(gpu.mem_used / max(gpu.mem_total, 1), show_value=False).classes(
                "grow"
            )
            ui.label(f"{fmt_bytes(gpu.mem_used)} of {fmt_bytes(gpu.mem_total)}").classes(
                "w-32 text-sm text-right"
            )
