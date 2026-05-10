"""FastAPI entrypoint for the personal investment panel."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.data_access import (
    dashboard_payload,
    load_config,
    load_panel_data,
    settings_payload,
    signals_payload,
    table_payload,
    ticker_payload,
)


APP_TITLE = "Personal Investment Panel"


def create_app() -> FastAPI:
    app = FastAPI(title=APP_TITLE)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        config, panel_data = _context()
        return settings_payload(config, panel_data)["status"]

    @app.get("/api/dashboard")
    def dashboard() -> dict[str, Any]:
        _, panel_data = _context()
        return dashboard_payload(panel_data)

    @app.get("/api/candidates")
    def candidates() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "candidates")

    @app.get("/api/signals")
    def signals() -> dict[str, Any]:
        _, panel_data = _context()
        return signals_payload(panel_data)

    @app.get("/api/tickers/{ticker}")
    def ticker_detail(ticker: str) -> dict[str, Any]:
        _, panel_data = _context()
        return ticker_payload(panel_data, ticker)

    @app.get("/api/portfolio")
    def portfolio() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "portfolio")

    @app.get("/api/theses")
    def theses() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "theses")

    @app.get("/api/trader-twins")
    def trader_twins() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "trader_twins")

    @app.get("/api/catalysts")
    def catalysts() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "catalysts")

    @app.get("/api/fundamentals")
    def fundamentals() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "fundamentals")

    @app.get("/api/disclosures")
    def disclosures() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "disclosures")

    @app.get("/api/source-health")
    def source_health() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "source_health")

    @app.get("/api/settings")
    def settings() -> dict[str, Any]:
        config, panel_data = _context()
        return settings_payload(config, panel_data)

    _mount_frontend(app)
    return app


def _context() -> tuple[dict[str, Any], Any]:
    config = load_config()
    return config, load_panel_data(config)


def _mount_frontend(app: FastAPI) -> None:
    dist_dir = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    index_path = dist_dir / "index.html"
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
        return FileResponse(index_path)


app = create_app()
