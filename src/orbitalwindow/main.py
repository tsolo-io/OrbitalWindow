# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Entry point: FastAPI app with the HTTP routes, NiceGUI mounted on top."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import docker
import uvicorn
from fastapi import FastAPI, Request, Response
from nicegui import ui

from . import __version__
from .api import router
from .config import Settings, get_settings
from .service import Service

log = logging.getLogger("orbitalwindow")

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    # allow-popups: "Open desktop" reuses a named tab, which "same-origin" would sever.
    "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
}


def create_app(settings: Settings | None = None, service: Service | None = None) -> FastAPI:
    """Create the FastAPI app with the HTTP routes and security headers (without the UI).

    Args:
        settings: Settings to use; read from the environment when None.
        service: An existing service (e.g. in tests); created at startup when None.

    Returns:
        The app.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Create and start the service if none was given, and stop it on shutdown.

        Args:
            app: The FastAPI app.

        Yields:
            Nothing; the app runs while suspended here.
        """
        svc = app.state.service
        if svc is None:
            client = docker.DockerClient(base_url=settings.docker_host, version="auto", timeout=120)
            svc = app.state.service = Service(settings, client)
            svc.bootstrap()
            svc.start_reconcile()
        from .ui.common import set_service

        set_service(svc)
        yield
        svc.stop()

    app = FastAPI(
        title="OrbitalWindow",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.service = service

    @app.middleware("http")
    async def security_headers(request: Request, call_next) -> Response:
        """Add the security headers, and ``Cache-Control: no-store`` to dynamic responses.

        Args:
            request: The incoming request.
            call_next: Handler for the rest of the middleware chain.

        Returns:
            The response with the headers added (existing ones are kept).
        """
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            # Pages under /desktop/ load in the named desktop tab; COOP there would clear its
            # name, and "Open desktop" could no longer reuse the tab.
            if key == "Cross-Origin-Opener-Policy" and request.url.path.startswith("/desktop/"):
                continue
            response.headers.setdefault(key, value)
        if request.url.path not in ("/healthz", "/auth/desktop"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.include_router(router)
    return app


def mount_ui(app: FastAPI, settings: Settings) -> None:
    """Register the NiceGUI pages and mount NiceGUI on the app.

    Args:
        app: The FastAPI app.
        settings: The process-level settings.
    """
    from .ui import (  # noqa: F401  (registers the pages)
        admin_addons,
        admin_groups,
        admin_images,
        admin_settings,
        admin_system,
        admin_users,
        ai,
        benchmarks,
        dashboard,
        help,
        login,
        status,
    )

    ui.run_with(
        app,
        title="OrbitalWindow",
        favicon="🛰️",
        show_welcome_message=False,
        reconnect_timeout=10.0,
    )


def build() -> FastAPI:
    """Build the complete app (HTTP routes and UI); the uvicorn factory.

    Returns:
        The app.
    """
    settings = get_settings()
    app = create_app(settings)
    mount_ui(app, settings)
    return app


def run() -> None:
    """Configure logging and serve the app with uvicorn."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    uvicorn.run(
        "orbitalwindow.main:build",
        factory=True,
        host=settings.listen_host,
        port=settings.listen_port,
        proxy_headers=True,
        # Only Traefik can reach the service (it is not published), so trust its X-Forwarded-For.
        forwarded_allow_ips="*",
        log_level="info",
    )


if __name__ == "__main__":
    run()
