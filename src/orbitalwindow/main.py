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
    "Cross-Origin-Opener-Policy": "same-origin",
}


def create_app(settings: Settings | None = None, service: Service | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.path not in ("/healthz", "/auth/desktop"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.include_router(router)
    return app


def mount_ui(app: FastAPI, settings: Settings) -> None:
    from .ui import (  # noqa: F401  (registers the pages)
        admin_addons,
        admin_images,
        admin_settings,
        admin_system,
        admin_users,
        dashboard,
        login,
    )

    ui.run_with(
        app,
        title="OrbitalWindow",
        favicon="🛰️",
        show_welcome_message=False,
        reconnect_timeout=10.0,
    )


def build() -> FastAPI:
    settings = get_settings()
    app = create_app(settings)
    mount_ui(app, settings)
    return app


def run() -> None:
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
