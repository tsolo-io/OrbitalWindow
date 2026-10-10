# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""The AI session card on the AI page: start a downloaded model with vLLM, follow it, stop it."""

from collections.abc import Awaitable, Callable
from typing import Any

from nicegui import run, ui

from ..aisession import SessionState
from ..fit import GIB
from ..service import Service
from ..users import ValidationError
from .common import PageAuth, guarded

STATUS_COLORS = {"running": "positive", "starting": "orange", "failed": "negative"}
LOADING = " · loading, this can take several minutes"


def session_card(auth: PageAuth, svc: Service) -> Callable[[], Awaitable[None]]:
    """Add the AI session card to the current container.

    Args:
        auth: Session of the page.
        svc: The application service.

    Returns:
        A coroutine function that refreshes the card (the page calls it on its timer).
    """
    actor = auth.user.name
    with ui.card().classes("w-full gap-2"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("AI session").classes("text-h6")
            badge = ui.badge("stopped", color="grey")
        status = ui.label().classes("text-body1")
        log_view = ui.code("").classes("w-full text-xs")
        log_view.set_visibility(False)
        stop_btn = ui.button("Stop the session", icon="stop", color="negative").props("no-caps")

        form = ui.column().classes("w-full gap-2")
        with form:
            ui.label(
                "Start a downloaded model. Everyone can then use it, and it keeps running until it "
                "is stopped. One model runs at a time."
            ).classes("text-caption text-grey-7")
            with ui.row().classes("items-end gap-4 w-full"):
                model_in = ui.select([], label="Model").classes("min-w-[20rem]")
                gpu_in = ui.select({}, label="GPU").classes("min-w-[16rem]")
                context_in = ui.number("Context (tokens)", value=8192, min=256, step=1024).classes(
                    "w-40"
                )
            with ui.row().classes("items-center gap-2 w-full"):
                ui.label("GPU memory for the model:")
                share_in = ui.slider(min=0.1, max=0.95, step=0.05, value=0.9).classes("w-64")
                share_label = ui.label()
            estimate_label = ui.label().classes("text-body2")
            start_btn = ui.button("Start", icon="play_arrow").props("no-caps")

        with ui.expansion("Use the model from code (OpenAI API)", icon="code").classes("w-full"):
            ui.label(
                "Programs talk to the model with the OpenAI API (for example the openai Python "
                "package). Inside the server, only Open WebUI and OrbitalWindow reach it. Switch "
                "this on to reach it from other computers, such as a desktop or a laptop."
            ).classes("text-caption text-grey-7")
            public_in = ui.switch("Reachable from other computers", value=svc.ai_api_public())
            api_url = ui.label().classes("font-mono")
            key_in = ui.input("API key", password=True, password_toggle_button=True).props(
                "readonly dense outlined"
            )
            key_in.classes("min-w-[28rem]")
            new_key_btn = ui.button("New key", icon="autorenew").props("no-caps flat")
            ui.label("Changes to these settings apply when the session is next started.").classes(
                "text-caption text-grey-7"
            )
            example = ui.code("", language="python").classes("w-full text-xs")

    gpu_total: dict[int, float] = {}

    def show_api(state: SessionState) -> None:
        """Show the API address, key and a Python example.

        Args:
            state: The session state (its model goes into the example).
        """
        url = svc.ai_api_url()
        api_url.set_text(url if svc.ai_api_public() else f"{url} (switched off)")
        key_in.set_value(svc.ai_api_key())
        model = state.repo or "<model>"
        example.set_content(
            "from openai import OpenAI\n\n"
            f'client = OpenAI(base_url="{url}", api_key="<API key>")\n'
            "reply = client.chat.completions.create(\n"
            f'    model="{model}",\n'
            '    messages=[{"role": "user", "content": "Hello!"}],\n'
            ")\nprint(reply.choices[0].message.content)\n"
            "# Self-signed certificate? Pass http_client=httpx.Client(verify=False)."
        )

    async def update_estimate() -> None:
        """Pre-fill the context and GPU share for the chosen model and show its estimate."""
        if not model_in.value:
            estimate_label.set_text("")
            return
        try:
            defaults = await run.io_bound(
                svc.session_defaults, model_in.value, int(context_in.value or 8192)
            )
        except ValidationError as exc:
            estimate_label.set_text(str(exc))
            return
        if defaults["max_context"]:
            context_in.props(f"max={defaults['max_context']}")
        context_in.set_value(defaults["context"])
        share_in.set_value(defaults["gpu_share"])
        estimate = defaults["estimate"]
        estimate_label.set_text(
            f"Needs about {estimate.total / GIB:.1f} GB for a {estimate.context:,}-token context. "
            f"{defaults['verdict'].text}"
        )

    def show_share() -> None:
        """Show the GPU share as a percentage and in GB of the chosen GPU."""
        total = gpu_total.get(gpu_in.value)
        amount = f" ({share_in.value * total / GIB:.1f} GB)" if total else ""
        share_label.set_text(f"{share_in.value:.0%}{amount}")

    async def refresh() -> None:
        """Reload the session state, its log, the model and GPU choices."""
        state, log_text = await run.io_bound(svc.ai_session)
        badge.set_text(state.status)
        badge.props(f"color={STATUS_COLORS.get(state.status, 'grey')}")
        if state.status in ("starting", "running"):
            started = state.started_at.strftime("%Y-%m-%d %H:%M") if state.started_at else ""
            status.set_text(
                f"{state.repo} on GPU {state.gpu_index} · {state.context:,} tokens · "
                f"{state.gpu_share:.0%} of the GPU · started by {state.started_by} {started}"
                + (LOADING if state.status == "starting" else "")
            )
        elif state.status == "failed":
            status.set_text(f"{state.repo} failed to run. The end of its log:")
        else:
            status.set_text("No model is running.")
        shown_log = log_text if state.status == "starting" else state.error
        log_view.set_content(shown_log)
        log_view.set_visibility(bool(shown_log))
        active = state.status in ("starting", "running")
        stop_btn.set_visibility(active)
        form.set_visibility(not active)
        if not active:
            library, _, _ = await run.io_bound(svc.model_library)
            options = [model.repo for model in library]
            if options != model_in.options:
                keep = model_in.value if model_in.value in options else None
                model_in.set_options(options, value=keep)
            gpus = await run.io_bound(svc.gpus.stats)
            gpu_total.clear()
            gpu_total.update({gpu.index: float(gpu.mem_total) for gpu in gpus})
            gpu_options = {
                gpu.index: f"{gpu.index}: {gpu.name} ({(gpu.mem_total - gpu.mem_used) / GIB:.1f} "
                f"of {gpu.mem_total / GIB:.1f} GB free)"
                for gpu in gpus
            }
            chosen = gpu_in.value if gpu_in.value in gpu_options else next(iter(gpu_options), None)
            gpu_in.set_options(gpu_options, value=chosen)
            show_share()
        show_api(state)

    async def act(fn: Callable[..., Any], *args: Any, done: str) -> None:
        """Run a service call as this user, show the outcome, then refresh.

        Args:
            fn: Service function called as ``fn(actor, *args)``.
            *args: Further arguments.
            done: Message on success.
        """
        try:
            await run.io_bound(fn, actor, *args)
        except ValidationError as exc:
            ui.notify(str(exc), type="negative", multi_line=True)
        else:
            ui.notify(done, type="positive")
        await refresh()

    async def on_start() -> None:
        """Start the session, asking first when the estimate says it may not fit."""
        if not model_in.value:
            ui.notify("Choose a model first.", type="warning")
            return
        defaults = await run.io_bound(
            svc.session_defaults, model_in.value, int(context_in.value or 8192)
        )
        if defaults["verdict"].level in ("busy", "tight", "too-big"):
            with ui.dialog() as dialog, ui.card():
                ui.label("Start anyway?").classes("text-h6")
                ui.label(defaults["verdict"].text)
                ui.label("If it doesn't fit, the session fails and shows why.").classes(
                    "text-grey-8"
                )
                with ui.row():
                    ui.button("Start", on_click=lambda: dialog.submit(True)).props("no-caps")
                    ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("no-caps flat")
            if not await dialog:
                return
        await act(
            svc.start_ai_session,
            model_in.value,
            gpu_in.value,
            context_in.value,
            share_in.value,
            done="Starting. Loading a model takes a few minutes.",
        )

    async def on_stop() -> None:
        """Ask for confirmation, then stop the session."""
        with ui.dialog() as dialog, ui.card():
            ui.label("Stop the AI session?").classes("text-h6")
            ui.label("Everyone using the model loses it until it is started again.")
            with ui.row():
                ui.button("Stop", color="negative", on_click=lambda: dialog.submit(True)).props(
                    "no-caps"
                )
                ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("no-caps flat")
        if await dialog:
            await act(svc.stop_ai_session, done="Stopped. The GPU memory is free again.")

    async def on_public(event) -> None:
        """Save the "reachable from other computers" switch.

        Args:
            event: The switch's value-change event.
        """
        await act(svc.set_ai_api_public, bool(event.value), done="Saved.")

    async def on_new_key() -> None:
        """Replace the API key."""
        await act(svc.new_ai_api_key, done="New key made. It applies at the next start.")

    model_in.on_value_change(guarded(auth, update_estimate))
    context_in.on("blur", guarded(auth, update_estimate))
    share_in.on_value_change(lambda _: show_share())
    gpu_in.on_value_change(lambda _: show_share())
    start_btn.on_click(guarded(auth, on_start))
    stop_btn.on_click(guarded(auth, on_stop))
    public_in.on_value_change(guarded(auth, on_public))
    new_key_btn.on_click(guarded(auth, on_new_key))
    return refresh
