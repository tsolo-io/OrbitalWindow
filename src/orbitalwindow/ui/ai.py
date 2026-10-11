# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""AI managers: the model library. Disk space, downloaded models, downloads from Hugging Face."""

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import run, ui

from ..users import ValidationError
from .common import authenticate, frame, guarded, service

MODEL_COLUMNS = [
    {"name": "repo", "label": "Model", "field": "repo", "align": "left"},
    {"name": "size", "label": "Size", "field": "size", "align": "right"},
    {"name": "modified", "label": "Downloaded", "field": "modified", "align": "left"},
    {"name": "path", "label": "Folder", "field": "path", "align": "left"},
    {"name": "actions", "label": "", "field": "actions", "align": "right"},
]
DOWNLOAD_COLUMNS = [
    {"name": "repo", "label": "Model", "field": "repo", "align": "left"},
    {"name": "status", "label": "Status", "field": "status", "align": "left"},
    {"name": "progress", "label": "Progress", "field": "progress", "align": "left"},
    {"name": "requested_by", "label": "By", "field": "requested_by", "align": "left"},
    {"name": "when", "label": "Started", "field": "when", "align": "left"},
    {"name": "actions", "label": "", "field": "actions", "align": "right"},
]
STATUS_COLORS = (
    "{done: 'positive', running: 'primary', queued: 'grey', cancelling: 'orange', "
    "cancelled: 'grey', failed: 'negative'}"
)


def gb(size: int | None) -> str:
    """Format a byte count in GB for the page.

    Args:
        size: Bytes, or None.

    Returns:
        For example ``15.2 GB``; ``–`` for None.
    """
    if size is None:
        return "–"
    return f"{size / 1e9:.1f} GB" if size >= 1e8 else f"{size / 1e6:.0f} MB"


@ui.page("/ai")
def ai_page(request: Request) -> RedirectResponse | None:
    """Render the AI page (model library) for AI managers and admins.

    Args:
        request: The incoming page request.

    Returns:
        A redirect if not logged in as an AI manager or admin, else None (the page is rendered).
    """
    auth = authenticate(request, ai=True)
    if isinstance(auth, RedirectResponse):
        return auth
    svc = service()
    actor = auth.user.name
    frame(auth, "AI")
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        ui.label("AI models").classes("text-h5")
        ui.label(
            "Models are downloaded from Hugging Face into a shared folder on the server. "
            "Downloads run in the background, one at a time; you can leave this page."
        ).classes("text-grey-8")

        with ui.card().classes("w-full"):
            ui.label("Storage").classes("text-h6")
            space_label = ui.label().classes("text-body1")
            space_bar = ui.linear_progress(value=0, show_value=False).props("size=12px rounded")

        with ui.card().classes("w-full"):
            ui.label("Download a model").classes("text-h6")
            ui.label(
                "Enter the model id from its Hugging Face page, for example Qwen/Qwen3-8B. "
                "Inspect shows its size before you download it."
            ).classes("text-caption text-grey-7")
            with ui.row().classes("items-end gap-2 w-full"):
                repo_in = (
                    ui.input("Model id", placeholder="Qwen/Qwen3-8B")
                    .props("dense outlined")
                    .classes("min-w-[18rem]")
                )
                rev_in = ui.input("Revision (optional)", placeholder="main").props("dense outlined")
                inspect_btn = ui.button("Inspect", icon="search").props("no-caps outline")
                download_btn = ui.button("Download", icon="download").props("no-caps")
            info_box = ui.column().classes("gap-1")

        ui.label("Downloads").classes("text-h6")
        downloads = ui.table(columns=DOWNLOAD_COLUMNS, rows=[], row_key="id").classes("w-full")
        downloads.add_slot(
            "body-cell-status",
            f"""
            <q-td :props="props">
              <q-badge :color="{STATUS_COLORS}[props.row.status] || 'grey'"
                       :label="props.row.status" />
              <div v-if="props.row.error" class="text-negative text-caption"
                   style="white-space: pre-wrap; max-width: 32rem">{{{{ props.row.error }}}}</div>
            </q-td>
            """,
        )
        downloads.add_slot(
            "body-cell-progress",
            """
            <q-td :props="props">
              <q-linear-progress :value="props.row.fraction" size="10px" rounded
                                 style="min-width: 10rem" />
              <div class="text-caption">{{ props.row.progress }}</div>
            </q-td>
            """,
        )
        downloads.add_slot(
            "body-cell-actions",
            """
            <q-td :props="props">
              <q-btn v-if="['queued', 'running'].includes(props.row.status)" flat dense no-caps
                     icon="close" label="Cancel" @click="$parent.$emit('cancel', props.row)" />
              <q-btn v-if="['failed', 'cancelled'].includes(props.row.status)" flat dense
                     no-caps icon="replay" label="Resume"
                     @click="$parent.$emit('resume', props.row)" />
            </q-td>
            """,
        )

        ui.label("Downloaded models").classes("text-h6")
        models = ui.table(columns=MODEL_COLUMNS, rows=[], row_key="repo").classes("w-full")
        models.add_slot(
            "body-cell-actions",
            """
            <q-td :props="props">
              <q-btn flat dense no-caps icon="delete" label="Delete" color="negative"
                     @click="$parent.$emit('delete', props.row)" />
            </q-td>
            """,
        )

        with ui.expansion("Hugging Face token", icon="key").classes("w-full"):
            ui.label(
                "Needed only for gated models such as Llama: accept the model's licence on its "
                "Hugging Face page with your account, then create a read token under Settings → "
                "Access Tokens. It is kept on the server and only used for downloads."
            ).classes("text-caption text-grey-7")
            token_state = ui.label().classes("text-body2")
            with ui.row().classes("items-end gap-2"):
                token_in = (
                    ui.input("Token", password=True, placeholder="hf_…")
                    .props("dense outlined")
                    .classes("min-w-[18rem]")
                )
                token_save = ui.button("Save token", icon="save").props("no-caps")
                token_clear = ui.button("Remove token", icon="delete").props("no-caps flat")

        def show_token_state() -> None:
            """Show whether a Hugging Face token is stored (never the token itself)."""
            token_state.set_text(
                "A token is stored." if svc.hf_token() else "No token stored (public models only)."
            )

        async def refresh() -> None:
            """Reload storage, downloads and the model list."""
            library, space, rows = await run.io_bound(svc.model_library)
            space_label.set_text(
                f"{gb(space.free)} free of {gb(space.total)} "
                f"· models use {gb(sum(m.bytes for m in library))}"
            )
            space_bar.set_value(space.used / space.total if space.total else 0)
            models.rows = [
                {
                    "repo": model.repo,
                    "size": gb(model.bytes),
                    "modified": model.modified.strftime("%Y-%m-%d %H:%M"),
                    "path": str(model.path),
                }
                for model in library
            ]
            models.update()
            downloads.rows = [
                {
                    "id": row["id"],
                    "repo": row["repo"],
                    "revision": row["revision"],
                    "status": row["status"],
                    "error": row["error"] if row["status"] == "failed" else "",
                    "fraction": row["bytes_done"] / row["bytes_total"] if row["bytes_total"] else 0,
                    "progress": f"{gb(row['bytes_done'])} of {gb(row['bytes_total'])}",
                    "requested_by": row["requested_by"],
                    "when": (row["started_at"] or row["created_at"]).strftime("%Y-%m-%d %H:%M"),
                }
                for row in rows
            ]
            downloads.update()

        async def act(fn, *args, done: str | None = None) -> bool:
            """Run a service call as this user, show errors or ``done``, then refresh.

            Args:
                fn: Service function called as ``fn(actor, *args)``.
                *args: Further arguments for ``fn``.
                done: Message to show on success; nothing if None.

            Returns:
                True if the call succeeded.
            """
            try:
                await run.io_bound(fn, actor, *args)
            except ValidationError as exc:
                ui.notify(str(exc), type="negative", multi_line=True)
                return False
            if done:
                ui.notify(done, type="positive")
            await refresh()
            return True

        async def on_inspect() -> None:
            """Look the model up on Hugging Face and show its size, licence and files."""
            info_box.clear()
            try:
                info = await run.io_bound(
                    svc.inspect_model, repo_in.value or "", rev_in.value or ""
                )
            except ValidationError as exc:
                ui.notify(str(exc), type="negative", multi_line=True)
                return
            with info_box:
                ui.label(info.repo).classes("text-subtitle1 font-medium")
                params = f"{info.parameters / 1e9:.1f} B parameters · " if info.parameters else ""
                ui.label(
                    f"{params}{info.architecture or 'unknown architecture'} · "
                    f"download {gb(info.total_bytes)} in {len(info.files)} files"
                    f"{' · licence ' + info.license if info.license else ''}"
                )
                if info.gated:
                    ui.label(
                        "Gated model: accept its licence on Hugging Face and store a token below."
                    ).classes("text-orange-9")
                if not info.has_safetensors:
                    ui.label(
                        "This model has no safetensors weights and can't be used by the AI session."
                    ).classes("text-negative")

        async def on_download() -> None:
            """Queue the download of the model in the form."""
            ok = await act(
                svc.download_model,
                repo_in.value or "",
                rev_in.value or "",
                done="Download queued. It runs in the background.",
            )
            if ok:
                info_box.clear()

        async def on_cancel(event) -> None:
            """Cancel a download.

            Args:
                event: Table event with the download row.
            """
            await act(svc.cancel_download, event.args["id"], done="Download cancelled.")

        async def on_resume(event) -> None:
            """Queue a failed or cancelled download again (same commit); it resumes.

            Args:
                event: Table event with the download row.
            """
            await act(
                svc.download_model,
                event.args["repo"],
                event.args["revision"],
                done="Download queued again.",
            )

        async def on_delete(event) -> None:
            """Ask for confirmation, then delete a downloaded model.

            Args:
                event: Table event with the model row.
            """
            row = event.args
            with ui.dialog() as dialog, ui.card():
                ui.label(f"Delete {row['repo']} ({row['size']})?")
                ui.label("It can be downloaded again later.").classes("text-grey-8")
                with ui.row():
                    ui.button("Delete", color="negative", on_click=lambda: dialog.submit(True)
                              ).props("no-caps")  # fmt: skip
                    ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("no-caps flat")
            if await dialog:
                await act(svc.delete_model, row["repo"], done="Deleted.")

        async def on_token_save() -> None:
            """Store the token from the form, then clear the field."""
            if await act(svc.set_hf_token, token_in.value or "", done="Token saved."):
                token_in.set_value("")
                show_token_state()

        async def on_token_clear() -> None:
            """Remove the stored token."""
            if await act(svc.set_hf_token, "", done="Token removed."):
                show_token_state()

        inspect_btn.on_click(guarded(auth, on_inspect))
        download_btn.on_click(guarded(auth, on_download))
        downloads.on("cancel", guarded(auth, on_cancel))
        downloads.on("resume", guarded(auth, on_resume))
        models.on("delete", guarded(auth, on_delete))
        token_save.on_click(guarded(auth, on_token_save))
        token_clear.on_click(guarded(auth, on_token_clear))
        show_token_state()
        ui.timer(3.0, guarded(auth, refresh))
        ui.timer(0.1, guarded(auth, refresh), once=True)
    return None
