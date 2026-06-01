"""FastAPI entrypoint for the personal investment panel."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from ipaddress import ip_address, ip_network
import json
from threading import RLock
import time
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.data_access import (
    database_path,
    dashboard_payload,
    load_config,
    load_market_panel_data,
    load_panel_data,
    panel_snapshot_payload,
    populate_watchlist_symbol_data,
    delete_portfolio_position,
    delete_watchlist_symbol,
    save_portfolio_position,
    save_watchlist_symbol,
    settings_payload,
    signals_payload,
    table_payload,
    ticker_payload,
)
from investment_panel.core.refresh_jobs import ALLOWLIST, execute_refresh_job, fail_running_jobs, refresh_job_rows, run_refresh_job, start_refresh_job
from investment_panel.core.brokers import build_and_persist_agent_recommendations, stage_paper_order
from investment_panel.core.config import load_config as load_core_config
from investment_panel.core.db import db, init_db
from investment_panel.core.sources import source_detail_payload, source_ingestion_audit


APP_TITLE = "Personal Investment Panel"


class PortfolioPositionInput(BaseModel):
    symbol: str
    quantity: float
    avg_cost: float
    purchase_date: str | None = None
    notes: str = ""


class WatchlistSymbolInput(BaseModel):
    symbol: str
    name: str | None = None
    asset_class: str = "equity"
    notes: str = ""


class PaperOrderInput(BaseModel):
    recommendation_id: str


CONTEXT_CACHE_TTL_SECONDS = 3.0
TAILSCALE_CGNAT = ip_network("100.64.0.0/10")
_CONTEXT_CACHE: dict[str, Any] = {"expires_at": 0.0, "config_key": None, "value": None}
_CONTEXT_LOCK = RLock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config = load_config()
    fail_running_jobs(database_path(config), "Server restarted before refresh job completed.")
    yield


def create_app() -> FastAPI:
    app = FastAPI(title=APP_TITLE, lifespan=lifespan)
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

    @app.get("/api/panel-snapshot")
    def panel_snapshot(scope: str = "dashboard", offset: int = 0, limit: int | None = None) -> dict[str, Any]:
        if scope == "market":
            config = load_config()
            return panel_snapshot_payload(load_market_panel_data(config), scope, offset=offset, limit=limit)
        _, panel_data = _context()
        return panel_snapshot_payload(panel_data, scope, offset=offset, limit=limit)

    @app.get("/api/decision-readiness")
    def decision_readiness() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "decision_readiness")

    @app.get("/api/candidates")
    def candidates() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "candidates")

    @app.get("/api/signals")
    def signals() -> dict[str, Any]:
        _, panel_data = _context()
        return signals_payload(panel_data)

    @app.get("/api/opportunities-ranked")
    def opportunities_ranked() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "opportunities_ranked")

    @app.get("/api/opportunity-sources")
    def opportunity_sources() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "opportunity_sources")

    @app.get("/api/discovered-universe")
    def discovered_universe() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "discovered_universe")

    @app.get("/api/decision-queue")
    def decision_queue() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "decision_queue")

    @app.get("/api/source-freshness")
    def source_freshness() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "source_freshness")

    @app.get("/api/symbol-decision-snapshots")
    def symbol_decision_snapshots() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "symbol_decision_snapshots")

    @app.get("/api/tickers/{ticker}")
    def ticker_detail(ticker: str) -> dict[str, Any]:
        _, panel_data = _context()
        return ticker_payload(panel_data, ticker)

    @app.get("/api/tickers/{ticker}/decision-snapshot")
    def ticker_decision_snapshot(ticker: str) -> dict[str, Any]:
        _, panel_data = _context()
        normalized = ticker.upper()
        rows = [
            row
            for row in table_payload(panel_data, "symbol_decision_snapshot")["rows"]
            if str(row.get("symbol") or "").upper() == normalized
        ]
        if rows:
            return rows[0]
        return {"symbol": normalized, "found": False}

    @app.get("/api/portfolio")
    def portfolio() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "portfolio")

    @app.post("/api/portfolio/positions")
    def save_position(position: PortfolioPositionInput) -> dict[str, Any]:
        config = load_config()
        try:
            saved = save_portfolio_position(config, position.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _invalidate_context_cache()
        _, panel_data = _context()
        return {"position": saved, "portfolio": table_payload(panel_data, "portfolio")}

    @app.delete("/api/portfolio/positions/{symbol}")
    def delete_position(symbol: str) -> dict[str, Any]:
        config = load_config()
        try:
            deleted = delete_portfolio_position(config, symbol)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _invalidate_context_cache()
        _, panel_data = _context()
        return {"position": deleted, "portfolio": table_payload(panel_data, "portfolio")}

    @app.get("/api/theses")
    def theses() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "theses")

    @app.get("/api/thesis-monitor")
    def thesis_monitor() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "thesis_monitor")

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

    @app.get("/api/sources")
    def sources() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "sources")

    @app.get("/api/sources/{source_id}")
    def source_detail(source_id: str) -> dict[str, Any]:
        config = load_config()
        init_db(database_path(config))
        with _CONTEXT_LOCK:
            with db(database_path(config), read_only=False) as con:
                return source_detail_payload(con, source_id)

    @app.get("/api/source-items")
    def source_items() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "source_items")

    @app.get("/api/source-runs")
    def source_runs() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "source_runs")

    @app.get("/api/ticker-source-signals")
    def ticker_source_signals() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "ticker_source_signals")

    @app.get("/api/source-ingestion-audit")
    def source_audit() -> dict[str, Any]:
        config = load_config()
        init_db(database_path(config))
        with _CONTEXT_LOCK:
            with db(database_path(config), read_only=False) as con:
                return source_ingestion_audit(con)

    @app.get("/api/quotes")
    def quotes() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "quotes")

    @app.get("/api/screener")
    def screener() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "screener")

    @app.get("/api/options-expiries")
    def options_expiries() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "options_expiries")

    @app.get("/api/options-chain")
    def options_chain() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "options_chain")

    @app.get("/api/options-payoff-scenarios")
    def options_payoff_scenarios() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "options_payoff_scenarios")

    @app.get("/api/news")
    def news() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "news")

    @app.get("/api/tradingview-symbol-search")
    def tradingview_symbol_search() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "tradingview_symbol_search")

    @app.get("/api/tradingview-watchlists")
    def tradingview_watchlists() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "tradingview_watchlists")

    @app.get("/api/tradingview-alerts")
    def tradingview_alerts() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "tradingview_alerts")

    @app.get("/api/tradingview-chart-state")
    def tradingview_chart_state() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "tradingview_chart_state")

    @app.get("/api/sepa")
    def sepa() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "sepa")

    @app.get("/api/liquidity")
    def liquidity() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "liquidity")

    @app.get("/api/correlations")
    def correlations() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "correlations")

    @app.get("/api/etf-premiums")
    def etf_premiums() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "etf_premiums")

    @app.get("/api/analyst-estimates")
    def analyst_estimates() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "analyst_estimates")

    @app.get("/api/earnings")
    def earnings() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "earnings")

    @app.get("/api/earnings-setups")
    def earnings_setups() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "earnings_setups")

    @app.get("/api/valuations")
    def valuations() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "valuations")

    @app.get("/api/technicals")
    def technicals() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "technicals")

    @app.get("/api/research-packets")
    def research_packets() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "research_packets")

    @app.get("/api/memos")
    def memos() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "ticker_memos")

    @app.get("/api/provider-runs")
    def provider_runs() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "provider_runs")

    @app.get("/api/broker/status")
    def broker_status() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "broker_status")

    @app.get("/api/broker/accounts")
    def broker_accounts() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "broker_accounts")

    @app.get("/api/broker/positions")
    def broker_positions() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "broker_positions")

    @app.get("/api/agent/recommendations")
    def agent_recommendations() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "agent_recommendations")

    @app.post("/api/agent/review")
    def run_agent_review(request: Request) -> dict[str, Any]:
        _require_local_request(request)
        config = load_core_config("config.yaml")
        init_db(config.database.duckdb_path)
        with db(config.database.duckdb_path, read_only=False) as con:
            rows = build_and_persist_agent_recommendations(con, config.data_sources.brokers.policy)
        _CONTEXT_CACHE.update({"expires_at": 0.0, "config_key": None, "value": None})
        return {"status": "ok", "count": len(rows), "rows": rows[:25]}

    @app.get("/api/paper-orders")
    def paper_orders() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "paper_orders")

    @app.get("/api/daily-brief")
    def daily_brief() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "daily_brief")

    @app.get("/api/feed")
    def feed() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "feed_signals")

    @app.get("/api/watchlist-screen")
    def watchlist_screen() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "universe_screen")

    @app.get("/api/watchlist/symbols")
    def watchlist_symbols() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "manual_watchlist")

    @app.post("/api/watchlist/symbols")
    def save_watchlist_symbol_endpoint(item: WatchlistSymbolInput, request: Request) -> dict[str, Any]:
        _require_local_request(request)
        config = load_config()
        try:
            saved = save_watchlist_symbol(config, item.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            refresh_result = populate_watchlist_symbol_data(config, saved["symbol"], saved.get("asset_class"))
        except Exception as exc:  # pragma: no cover - defensive API boundary
            refresh_result = {"status": "error", "symbol": saved["symbol"], "errors": {"refresh": f"{type(exc).__name__}: {exc}"}}
        _invalidate_context_cache()
        return {"watchlist_symbol": saved, "data_refresh": refresh_result, "watchlist": {"rows": [], "count": 0}}

    @app.delete("/api/watchlist/symbols/{symbol}")
    def delete_watchlist_symbol_endpoint(symbol: str, request: Request) -> dict[str, Any]:
        _require_local_request(request)
        config = load_config()
        try:
            deleted = delete_watchlist_symbol(config, symbol)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _invalidate_context_cache()
        return {"watchlist_symbol": deleted, "watchlist": {"rows": [], "count": 0}}

    @app.get("/api/source-consensus")
    def source_consensus() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "source_consensus")

    @app.get("/api/ownership-consensus")
    def ownership_consensus() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "ownership_consensus")

    @app.get("/api/market-context")
    def market_context() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "market_context")

    @app.get("/api/portfolio-risk/exposure-clusters")
    def portfolio_risk_exposure_clusters() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "exposure_clusters")

    @app.get("/api/portfolio-risk/correlation-edges")
    def portfolio_risk_correlation_edges() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "correlation_edges")

    @app.get("/api/portfolio-risk/cards")
    def portfolio_risk_cards() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "portfolio_risk_cards")

    @app.get("/api/portfolio-risk/review-actions")
    def portfolio_risk_review_actions() -> dict[str, Any]:
        _, panel_data = _context()
        return table_payload(panel_data, "review_actions")

    @app.post("/api/paper-orders")
    def stage_paper_order_endpoint(payload: PaperOrderInput, request: Request) -> dict[str, Any]:
        _require_local_request(request)
        config = load_core_config("config.yaml")
        init_db(config.database.duckdb_path)
        try:
            with db(config.database.duckdb_path, read_only=False) as con:
                result = stage_paper_order(con, payload.recommendation_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _CONTEXT_CACHE.update({"expires_at": 0.0, "config_key": None, "value": None})
        return result

    @app.get("/api/refresh-jobs")
    def refresh_jobs() -> dict[str, Any]:
        config = load_config()
        rows = refresh_job_rows(database_path(config))
        return {"rows": rows, "count": len(rows), "allowlist": sorted(ALLOWLIST), "latest_status": _full_market_refresh_status(config)}

    @app.post("/api/refresh-jobs/{job_name}")
    def launch_refresh_job(job_name: str, request: Request) -> dict[str, Any]:
        _require_local_request(request)
        config = load_config()
        try:
            result = run_refresh_job(job_name, database_path(config), "config.yaml")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _invalidate_context_cache()
        return result

    @app.post("/api/refresh-jobs/{job_name}/background")
    def launch_refresh_job_background(job_name: str, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
        _require_local_request(request)
        config = load_config()
        db_path = database_path(config)
        try:
            job = start_refresh_job(job_name, db_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if job.get("created"):
            background_tasks.add_task(_execute_background_refresh_job, job["id"], job_name, db_path)
        return job

    @app.get("/api/settings")
    def settings() -> dict[str, Any]:
        config, panel_data = _context()
        return settings_payload(config, panel_data)

    _mount_frontend(app)
    return app


def _context() -> tuple[dict[str, Any], Any]:
    with _CONTEXT_LOCK:
        config = load_config()
        config_key = str(database_path(config))
        now = time.monotonic()
        cached = _CONTEXT_CACHE.get("value")
        if cached is not None and _CONTEXT_CACHE.get("config_key") == config_key and now < float(_CONTEXT_CACHE.get("expires_at") or 0):
            return cached
        value = (config, load_panel_data(config))
        _CONTEXT_CACHE.update({"value": value, "config_key": config_key, "expires_at": now + CONTEXT_CACHE_TTL_SECONDS})
        return value


def _invalidate_context_cache() -> None:
    _CONTEXT_CACHE.update({"expires_at": 0.0, "config_key": None, "value": None})


def _execute_background_refresh_job(job_id: str, job_name: str, db_path: Path) -> None:
    try:
        execute_refresh_job(job_id, job_name, db_path, "config.yaml", raise_on_error=False)
    finally:
        _invalidate_context_cache()


def _full_market_refresh_status(config: dict[str, Any]) -> dict[str, Any] | None:
    status_dir = Path(config.get("nas", {}).get("status_dir", "/Volumes/agent/data-sources/status"))
    status_path = status_dir / "mini-market-full-refresh.json"
    if not status_path.exists():
        return None
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _require_local_request(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return
    try:
        address = ip_address(host)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Write actions are available only from the local network.") from exc
    if not (address.is_loopback or address.is_private or address.is_link_local or address in TAILSCALE_CGNAT):
        raise HTTPException(status_code=403, detail="Write actions are available only from the local network.")


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
