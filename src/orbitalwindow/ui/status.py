# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Server status for every user: load, CPU, memory, disks, disk and network traffic, GPUs.

The figures come from node-exporter on the host and NVML. They are read only while this page is
open, by the page itself; nothing is cached or polled in the background.
"""

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import run, ui

from ..hoststats import NodeExporterError, Sample, rates
from .common import authenticate, frame, guarded, service

REFRESH_SECONDS = 5.0


def size(value: float | None) -> str:
    """Format bytes for the page.

    Args:
        value: Bytes, or None.

    Returns:
        For example ``12.3 GB``; ``–`` for None.
    """
    if value is None:
        return "–"
    for unit, factor in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("kB", 1024)):
        if value >= factor:
            return f"{value / factor:.1f} {unit}"
    return f"{value:.0f} B"


def duration(seconds: float | None) -> str:
    """Format an uptime.

    Args:
        seconds: Seconds, or None.

    Returns:
        For example ``12 days 3 h``; ``–`` for None.
    """
    if seconds is None:
        return "–"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days} day{'s' if days != 1 else ''} {hours} h"
    return f"{hours} h {rest // 60} min"


def bar(label: str, fraction: float, text: str) -> None:
    """Add a labelled progress bar, orange from 75 % and red from 90 %.

    Args:
        label: What is measured.
        fraction: Used share from 0 to 1.
        text: The figures shown under the bar.
    """
    color = "negative" if fraction >= 0.9 else "orange" if fraction >= 0.75 else "primary"
    with ui.column().classes("w-full gap-0"):
        with ui.row().classes("w-full justify-between"):
            ui.label(label).classes("text-body2 font-medium")
            ui.label(f"{fraction * 100:.0f} %").classes("text-body2")
        ui.linear_progress(value=fraction, show_value=False, color=color).props("size=10px rounded")
        ui.label(text).classes("text-caption text-grey-7")


@ui.page("/status")
def status_page(request: Request) -> RedirectResponse | None:
    """Render the server status page.

    Args:
        request: The incoming page request.

    Returns:
        A redirect if not logged in, else None (the page is rendered).
    """
    auth = authenticate(request)
    if isinstance(auth, RedirectResponse):
        return auth
    svc = service()
    frame(auth, "Server status")
    previous: list[Sample] = []
    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        ui.label("Server status").classes("text-h5")
        ui.label(
            f"How busy the server is right now. Updated every {REFRESH_SECONDS:.0f} seconds "
            "while this page is open."
        ).classes("text-grey-8")
        body = ui.column().classes("w-full gap-4")

        def render(current: Sample | None, error: str, gpus: list) -> None:
            """Redraw the page from the latest sample (and the one before, for rates).

            Args:
                current: The latest sample, or None if node-exporter could not be read.
                error: Why it could not be read.
                gpus: GPU statistics from NVML (empty without NVIDIA GPUs).
            """
            body.clear()
            with body:
                if current is None:
                    with ui.card().classes("w-full border-l-4 border-negative"):
                        ui.label("Server figures are not available").classes("text-h6")
                        ui.label(
                            "OrbitalWindow reads them from Prometheus node-exporter on the "
                            "server, which isn't reachable. An administrator needs to install "
                            "and start it (see the admin guide: Installing)."
                        )
                        if auth.user.is_admin:
                            ui.label(f"{svc.node_exporter.url}: {error}").classes(
                                "text-caption text-negative"
                            )
                else:
                    overview(current)
                rate = rates(previous[0], current) if previous and current else None
                if current is not None:
                    usage(current, rate)
                if gpus:
                    gpu_card(gpus)
                if current is not None:
                    traffic(rate)

        def overview(current: Sample) -> None:
            """Add the host name, uptime and load card.

            Args:
                current: The latest sample.
            """
            with ui.card().classes("w-full"):
                ui.label(current.hostname or "Server").classes("text-h6")
                ui.label(
                    f"{current.os_name} · up {duration(current.uptime)} · {current.cpus} CPUs"
                ).classes("text-grey-8")
                if current.load:
                    one, five, fifteen = current.load
                    busy = one / current.cpus if current.cpus else 0
                    word = "quiet" if busy < 0.5 else "busy" if busy < 1 else "overloaded"
                    ui.label(
                        f"Load {one:.2f} · {five:.2f} · {fifteen:.2f} (1, 5 and 15 minutes): {word}"
                    ).classes("text-body1")

        def usage(current: Sample, rate) -> None:
            """Add the CPU, memory and disk space card.

            Args:
                current: The latest sample.
                rate: Rates since the previous sample, or None on the first read.
            """
            with ui.card().classes("w-full gap-3"):
                ui.label("Usage").classes("text-h6")
                if rate is not None:
                    bar("CPU", rate.cpu, f"{current.cpus} CPUs")
                else:
                    ui.label("CPU: measuring…").classes("text-body2 text-grey-7")
                if current.mem_total and current.mem_available is not None:
                    used = current.mem_total - current.mem_available
                    bar(
                        "Memory",
                        used / current.mem_total,
                        f"{size(used)} used of {size(current.mem_total)}",
                    )
                if current.swap_total:
                    swap_used = current.swap_total - (current.swap_free or 0)
                    bar(
                        "Swap",
                        swap_used / current.swap_total,
                        f"{size(swap_used)} used of {size(current.swap_total)}",
                    )
                for fs in current.filesystems:
                    bar(
                        f"Disk {fs.mountpoint}",
                        fs.used_fraction,
                        f"{size(fs.avail)} free of {size(fs.size)} ({fs.device})",
                    )

        def gpu_card(gpus: list) -> None:
            """Add the GPU card.

            Args:
                gpus: GPU statistics from NVML.
            """
            with ui.card().classes("w-full gap-3"):
                ui.label("GPUs").classes("text-h6")
                for gpu in gpus:
                    bar(f"GPU {gpu.index}: {gpu.name}", gpu.util_percent / 100, "load")
                    bar(
                        f"GPU {gpu.index} memory",
                        gpu.mem_used / gpu.mem_total if gpu.mem_total else 0,
                        f"{size(gpu.mem_used)} used of {size(gpu.mem_total)}",
                    )

        def traffic(rate) -> None:
            """Add the disk and network traffic card.

            Args:
                rate: Rates since the previous sample, or None on the first read.
            """
            with ui.card().classes("w-full"):
                ui.label("Disk and network traffic").classes("text-h6")
                if rate is None:
                    ui.label("Measuring…").classes("text-grey-7")
                    return
                rows = [
                    {
                        "device": f"Disk {disk.name}",
                        "in": f"{size(disk.read)}/s read",
                        "out": f"{size(disk.write)}/s written",
                        "busy": f"{disk.busy * 100:.0f} % busy",
                    }
                    for disk in rate.disks
                ] + [
                    {
                        "device": f"Network {net.name}",
                        "in": f"{size(net.read)}/s in",
                        "out": f"{size(net.write)}/s out",
                        "busy": "",
                    }
                    for net in rate.networks
                ]
                columns = [
                    {"name": key, "label": "", "field": key, "align": "left"}
                    for key in ("device", "in", "out", "busy")
                ]
                ui.table(columns=columns, rows=rows, row_key="device").props(
                    "flat dense hide-header hide-bottom"
                ).classes("w-full")

        async def refresh() -> None:
            """Read node-exporter and NVML once and redraw."""
            error = ""
            try:
                current = await run.io_bound(svc.node_exporter.read)
            except NodeExporterError as exc:
                current, error = None, str(exc)
            else:
                if current is None:  # None: the page closed or the app is stopping
                    return
            gpus = await run.io_bound(svc.gpus.stats)
            render(current, error, gpus)
            if current is not None:
                previous[:] = [current]

        ui.timer(REFRESH_SECONDS, guarded(auth, refresh))
        ui.timer(0.1, guarded(auth, refresh), once=True)
    return None
