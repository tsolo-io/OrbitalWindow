# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Admin: settings. Defaults for new users, scratch retention, host name, GPU vendor, AI engine."""

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
ENGINE_OPTIONS = {"vllm": "vLLM", "llamacpp": "llama.cpp"}


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
            ui.label(
                "Applies to desktops started after the change. A change rebuilds every desktop "
                "image (Intel images get Intel's GPU computing packages)."
            ).classes("text-caption text-grey-7")
        with ui.card().classes("w-full"):
            ui.label("AI").classes("text-h6")
            engine = ui.select(
                ENGINE_OPTIONS, label="AI engine", value=current.get("ai_engine", "vllm")
            )
            ui.label(
                "The program that runs the AI session. vLLM loads safetensors models and serves "
                "many people fast on NVIDIA GPUs. llama.cpp loads GGUF models (compressed, so "
                "larger models fit) and runs on NVIDIA, Intel and AMD GPUs."
            ).classes("text-caption text-grey-7")
            ui.label(
                "vLLM does not work on Intel GPUs: on an Arc A770 a longer chat hangs the GPU. "
                "Use llama.cpp on Intel."
            ).classes("text-caption text-orange-9")
            ui.label(
                "Applies when the AI session is next started. Each engine needs its own model "
                "format: download a GGUF version of a model for llama.cpp."
            ).classes("text-caption text-grey-7")
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
                "ai_engine": engine.value,
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
