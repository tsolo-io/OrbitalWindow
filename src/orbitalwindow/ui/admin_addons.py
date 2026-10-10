# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Admin: add-ons. Loaded definitions, validation errors, enable/disable, restricted, reload.

Per-user grants of restricted add-ons are set on the users page.
"""

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import run, ui

from .common import authenticate, frame, guarded, service


@ui.page("/admin/addons")
def admin_addons(request: Request) -> RedirectResponse | None:
    """Render the admin add-ons page.

    Args:
        request: The incoming page request.

    Returns:
        A redirect if not logged in as an admin, else None (the page is rendered).
    """
    auth = authenticate(request, admin=True)
    if isinstance(auth, RedirectResponse):
        return auth
    svc = service()
    actor = auth.user.name
    frame(auth, "Add-ons")
    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Add-ons").classes("text-h5")
            reload_btn = ui.button("Reload add-ons", icon="refresh").props("no-caps")
        ui.label(
            "Add-ons are folders in addons/ on the server. Users switch on open add-ons "
            "themselves; restricted ones (such as MATLAB) must first be granted on the Users page."
        ).classes("text-grey-8")
        body = ui.column().classes("w-full gap-3")

        def render() -> None:
            """Rebuild the list of loaded add-ons with their switches, then the invalid ones."""
            body.clear()
            settings = {r["key"]: r for r in svc.db.all("SELECT * FROM addon_settings")}
            catalog = svc.software.catalog
            with body:
                for key, addon in sorted(catalog.addons.items()):
                    spec = addon.spec
                    row = settings.get(key, {})
                    restricted = svc.software.is_restricted(addon, row)
                    with ui.card().classes("w-full"):
                        with ui.row().classes("w-full items-center justify-between"):
                            with ui.row().classes("items-center gap-2"):
                                ui.label(spec.label).classes("text-subtitle1 font-medium")
                                ui.badge(key, color="grey-6")
                                ui.badge("valid", color="positive")
                            with ui.row().classes("items-center gap-4"):
                                enabled = ui.switch("Offered", value=row.get("enabled", True))
                                restr = ui.switch("Restricted", value=restricted)
                        ui.label(spec.description).classes("text-caption text-grey-8")
                        parts = []
                        if spec.apt:
                            parts.append(f"{len(spec.apt)} packages")
                        if spec.script:
                            parts.append(f"script {spec.script}")
                        if spec.launchers:
                            parts.append(f"{len(spec.launchers)} launcher(s)")
                        parts += [f"mount {m.source} → {m.target}" for m in spec.mounts]
                        if spec.env:
                            parts.append("env " + ", ".join(spec.env))
                        ui.label(" · ".join(parts) or "no contents").classes("text-caption")
                    enabled.on_value_change(guarded(auth, _set(svc.set_addon_enabled, key)))
                    restr.on_value_change(guarded(auth, _set(svc.set_addon_restricted, key)))
                for err in catalog.errors:
                    with ui.card().classes("w-full border-l-4 border-negative"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(err.key).classes("text-subtitle1 font-medium")
                            ui.badge("error – skipped", color="negative")
                        ui.label(err.error).classes("text-sm text-negative")

        def _set(fn, key: str):
            """Make a switch handler that saves one add-on flag.

            Args:
                fn: Service setter called as ``fn(actor, key, value)``.
                key: Add-on key.

            Returns:
                An async value-change handler.
            """

            async def handler(event) -> None:
                """Save the switch's new value.

                Args:
                    event: The value-change event of the switch.
                """
                await run.io_bound(fn, actor, key, bool(event.value))
                ui.notify("Saved.")

            return handler

        async def on_reload() -> None:
            """Reload the add-on definitions from disk, re-render and report errors."""
            errors = await run.io_bound(svc.reload_addons, actor)
            render()
            if errors:
                ui.notify(f"Reloaded. {len(errors)} add-on(s) have errors.", type="warning")
            else:
                ui.notify("Reloaded. All add-ons are valid.", type="positive")

        reload_btn.on_click(guarded(auth, on_reload))
        render()
    return None
