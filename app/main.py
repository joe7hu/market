"""FastAPI entrypoint for the personal investment panel.

Thin app factory: it builds the FastAPI app, includes the routers under
`app/routers/`, and mounts the built frontend. Route logic lives in the routers;
domain owners live in their owning modules.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import dependencies
from app.routers import ALL_ROUTERS
from app.scheduler import run_scheduler, scheduler_enabled
from investment_panel.core.refresh_jobs import mark_stale_running_jobs
from investment_panel.database.authority import close_cached_runtimes, database_url

APP_TITLE = "Personal Investment Panel"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config_provider = _app.dependency_overrides.get(dependencies.get_config, dependencies.get_config)
    config = config_provider()
    dsn = database_url(config)
    try:
        mark_stale_running_jobs(dsn)
    except Exception:
        logging.getLogger("market.startup").exception("could not reconcile stale refresh jobs")
    scheduler_task: asyncio.Task | None = None
    if scheduler_enabled():
        scheduler_task = asyncio.create_task(run_scheduler(dsn))
    else:
        logging.getLogger("market.scheduler").info("market scheduler disabled via MARKET_SCHEDULER_ENABLED")
    try:
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass
        close_cached_runtimes()


def create_app() -> FastAPI:
    app = FastAPI(title=APP_TITLE, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for router in ALL_ROUTERS:
        app.include_router(router)

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    dist_dir = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    index_path = dist_dir / "index.html"
    index_headers = {"Cache-Control": "no-cache"}
    if not index_path.exists():
        return

    assets_dir = dist_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str = "") -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
        requested = dist_dir / path
        if requested.is_file():
            return FileResponse(requested)
        return FileResponse(index_path, headers=index_headers)

    @app.api_route(
        "/{path:path}",
        methods=["POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    def frontend_non_get(path: str = "") -> None:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
        raise HTTPException(status_code=405, detail="Method not allowed")


app = create_app()
