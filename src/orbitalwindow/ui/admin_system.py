# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Admin: system. Every desktop with its state and CPU/RAM use, the GPUs, links to cAdvisor,
and the audit log."""

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import run, ui

from ..metrics import fmt_bytes
from .common import authenticate, frame, service

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
    {"name": "measured", "label": "Measured (UTC)", "field": "measured", "align": "right"},
]
AUDIT_COLUMNS = [
    {"name": "ts", "label": "Time (UTC)", "field": "ts", "align": "left"},
    {"name": "actor", "label": "Who", "field": "actor", "align": "left"},
    {"name": "action", "label": "Action", "field": "action", "align": "left"},
    {"name": "target", "label": "Target", "field": "target", "align": "left"},
    {"name": "detail", "label": "Detail", "field": "detail", "align": "left"},
]


@ui.page("/admin/system")
def admin_system(request: Request) -> RedirectResponse | None:
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
            usage, host = await run.io_bound(svc.disk_overview)
            free = fmt_bytes(host.free)
            text = f"Free on the server: {free} of {fmt_bytes(host.total)}"
            if host.warning:
                text += f" — the disk is {host.used_fraction:.0%} full. Free up space soon."
            host_label.set_text(text)
            host_label.classes(replace="text-negative" if host.warning else "text-grey-8")
            disk.rows = [
                {
                    "key": f"{u.subject}:{u.name}",
                    "subject": u.subject,
                    "name": u.name,
                    "used": fmt_bytes(u.bytes),
                    "quota": f"{u.quota_gb:g} GB" if u.quota_gb else "–",
                    "percent": f"{u.fraction:.0%}" if u.fraction is not None else "–",
                    "warning": u.warning,
                    "measured": u.measured_at.strftime("%Y-%m-%d %H:%M") if u.measured_at else "",
                }
                for u in usage
            ]
            disk.update()

        async def on_measure() -> None:
            measure_btn.disable()
            try:
                await run.io_bound(svc.measure_disk)
            finally:
                measure_btn.enable()
            await refresh_disk()
            ui.notify("Disk usage measured.")

        measure_btn.on_click(on_measure)
        ui.timer(0.1, refresh_disk, once=True)
        ui.timer(60.0, refresh_disk)

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
            rows = await run.io_bound(svc.system_overview)
            cadvisor_note.set_visibility(not svc.metrics.available)
            desktops.rows = [
                {
                    "name": r["name"] + (" (disabled)" if r["disabled"] else ""),
                    "state": r["state"],
                    "image": r["image_label"] or "–",
                    "cpu": f"{r['cpu_cores']:.2f} cores" if r["cpu_cores"] is not None else "–",
                    "cpu_limit": f"{r['cpus']:g}" if r["cpus"] else "unlimited",
                    "mem": fmt_bytes(r["mem_bytes"]) if r["mem_bytes"] is not None else "–",
                    "mem_limit": f"{r['mem_gb']:g} GB" if r["mem_gb"] else "unlimited",
                }
                for r in rows
            ]
            desktops.update()
            gpus = await run.io_bound(svc.gpus.stats)
            gpu_box.clear()
            with gpu_box:
                if not gpus:
                    ui.label("No GPU statistics (no NVIDIA GPU, or NVML is not available).")\
                        .classes("text-grey-7")  # fmt: skip
                for g in gpus:
                    gpu_line(g)

        async def refresh_audit() -> None:
            rows = await run.io_bound(svc.audit_log, search.value or "", 500)
            audit.rows = [
                r | {"i": i, "ts": r["ts"].strftime("%Y-%m-%d %H:%M:%S")}
                for i, r in enumerate(rows)
            ]
            audit.update()

        search.on_value_change(refresh_audit)
        ui.timer(5.0, refresh)
        ui.timer(0.1, refresh, once=True)
        ui.timer(0.1, refresh_audit, once=True)
        ui.timer(30.0, refresh_audit)
    return None


def gpu_line(g) -> None:
    with ui.column().classes("w-full gap-1"):
        ui.label(f"GPU {g.index}: {g.name}").classes("font-medium")
        with ui.row().classes("w-full items-center no-wrap"):
            ui.label("Load").classes("w-16 text-sm")
            ui.linear_progress(g.util_percent / 100, show_value=False).classes("grow")
            ui.label(f"{g.util_percent}%").classes("w-32 text-sm text-right")
        with ui.row().classes("w-full items-center no-wrap"):
            ui.label("Memory").classes("w-16 text-sm")
            ui.linear_progress(g.mem_used / max(g.mem_total, 1), show_value=False).classes("grow")
            ui.label(f"{fmt_bytes(g.mem_used)} of {fmt_bytes(g.mem_total)}").classes(
                "w-32 text-sm text-right"
            )
