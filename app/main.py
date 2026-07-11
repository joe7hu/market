"""FastAPI entrypoint for the personal investment panel.

Thin app factory: it builds the FastAPI app, includes the routers under
`app/routers/`, and mounts the built frontend. Route logic lives in the routers;
shared service helpers/loaders/models live in `app/deps.py`. Add a new route by
adding a router under `app/routers/` and registering it in `app.routers.ALL_ROUTERS`
— do not grow this file back into a god-module.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import deps
from app.routers import ALL_ROUTERS
from app.scheduler import run_scheduler, scheduler_enabled
from investment_panel.core.refresh_jobs import fail_running_jobs

# Re-exported for tests and any caller importing from app.main.
from app.deps import _invalidate_context_cache, _require_local_request  # noqa: F401


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config = deps.load_config()
    db_path = deps.database_path(config)
    fail_running_jobs(db_path, "Server restarted before refresh job completed.")
    scheduler_task: asyncio.Task | None = None
    if scheduler_enabled():
        scheduler_task = asyncio.create_task(run_scheduler(db_path))
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


def create_app() -> FastAPI:
    app = FastAPI(title=deps.APP_TITLE, lifespan=lifespan)
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
        requested = dist_dir / path
        if requested.is_file():
            return FileResponse(requested)
        return FileResponse(index_path, headers=index_headers)


app = create_app()
