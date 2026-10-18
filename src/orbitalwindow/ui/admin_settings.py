# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Admin: settings. Defaults for new users, scratch retention, host name, GPU vendor."""

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import run, ui

from ..users import ValidationError
from .common import authenticate, frame, guarded, service

GPU_OPTIONS = {
    "nvidia": "NVIDIA",
    "amd": "AMD",
    "intel": "Intel",
    "none": "None (no GPU, for testing only)",
}


@ui.page("/admin/settings")
def admin_settings(request: Request) -> RedirectResponse | None:
    """Render the admin settings page.

    Args:
        request: The incoming page request.

    Returns:
        A redirect if not logged in as an admin, else None (the page is rendered).
    """
    auth = authenticate(request, admin=True)
    if isinstance(auth, RedirectResponse):
        return auth
    svc = service()
    current = svc.get_settings()
    frame(auth, "Settings")
    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-4"):
        ui.label("Settings").classes("text-h5")
        with ui.card().classes("w-full"):
            ui.label("Defaults for new users").classes("text-h6")
            ui.label("Leave CPU or RAM empty for unlimited. Existing users keep their limits.")\
                .classes("text-caption text-grey-7")  # fmt: skip
            with ui.row().classes("w-full"):
                cpus = ui.input("CPUs", value=current.get("default_cpus", ""))
                mem = ui.input("RAM (GB)", value=current.get("default_mem_gb", ""))
                quota = ui.input("Disk quota (GB)", value=current.get("default_quota_gb", ""))
        with ui.card().classes("w-full"):
            ui.label("Server").classes("text-h6")
            host = ui.input("Host name users connect to", value=current.get("hostname", ""))
            ui.label(
                "Shown on the dashboard, for example in \\\\host\\user for Windows Explorer."
            ).classes("text-caption text-grey-7")
            gpu = ui.select(GPU_OPTIONS, label="GPU", value=current.get("gpu_vendor", "nvidia"))
            ui.label("Applies to desktops started after the change.").classes(
                "text-caption text-grey-7"
            )
        with ui.card().classes("w-full"):
            ui.label("Shared scratch folder").classes("text-h6")
            scratch = ui.input(
                "Delete files untouched for (days)", value=current.get("scratch_retention_days", "")
            )

        async def save() -> None:
            """Save all settings from the form and report the outcome."""
            values = {
                "default_cpus": cpus.value,
                "default_mem_gb": mem.value,
                "default_quota_gb": quota.value,
                "hostname": host.value,
                "gpu_vendor": gpu.value,
                "scratch_retention_days": scratch.value,
            }
            try:
                await run.io_bound(svc.update_settings, auth.user.name, values)
            except ValidationError as exc:
                ui.notify(str(exc), type="negative")
                return
            ui.notify("Settings saved.", type="positive")

        ui.button("Save settings", icon="save", on_click=guarded(auth, save)).props("no-caps")
    return None
