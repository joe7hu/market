"""Shared app service layer for the FastAPI routers.

Routers import this module as `deps` and reference every app-service name as
`deps.X` (helpers, loaders, db, domain functions, models, constants). This keeps
a single seam: tests patch `app.deps.<name>` and every route reader resolves
through this namespace. Import from here; do not re-grow `app/main.py` with route
logic — add a router under `app/routers/` instead.
"""
from __future__ import annotations

import json
import time
from ipaddress import ip_address, ip_network
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from fastapi import HTTPException, Request
from pydantic import BaseModel

from app.data_access import (
    database_path,
    dashboard_payload,
    load_config,
    load_market_panel_data,
    load_panel_data,
    load_panel_scope_data,
    load_table_panel_data,
    load_ticker_panel_data,
    panel_snapshot_payload,
    panel_contract_payload,
    populate_watchlist_symbol_data,
    delete_portfolio_position,
    delete_watchlist_symbol,
    save_portfolio_position,
    save_watchlist_symbol,
    settings_payload,
    signals_payload,
    table_payload,
    ticker_payload,
    update_agent_settings_config,
)
from investment_panel.core.refresh_jobs import ALLOWLIST, execute_refresh_job, refresh_job_rows, run_refresh_job, start_refresh_job
from investment_panel.core.brokers import build_and_persist_agent_recommendations, stage_paper_order
from investment_panel.core.config import load_config as load_core_config
from investment_panel.core.db import db, init_db
from investment_panel.core.option_agent_postmortem import AgentPostmortemValidationError, upsert_agent_postmortem
from investment_panel.core.option_agent_thesis import AgentThesisValidationError, refresh_option_agent_work, upsert_agent_thesis
from investment_panel.core.options_radar import (
    DEFAULT_STRATEGY_VERSION,
    StrategyPromotionError,
    acknowledge_radar_alert,
    promote_strategy_mutation,
    record_trade_journal_entry,
    refresh_strategy_proposal_evaluations,
)
from investment_panel.core.sources import source_detail_payload, source_ingestion_audit


APP_TITLE = "Personal Investment Panel"
CONTEXT_CACHE_TTL_SECONDS = 3.0
SOURCE_FRESHNESS_DEFAULT_LIMIT = 100
TAILSCALE_CGNAT = ip_network("100.64.0.0/10")
_CONTEXT_CACHE: dict[str, Any] = {"entries": {}, "expires_at": 0.0, "config_key": None, "value": None}
_CONTEXT_LOCK = RLock()


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


class StrategyPromotionInput(BaseModel):
    approved_by: str = "joe"


class AgentCommandSettingsInput(BaseModel):
    enabled: bool | None = None
    command: str | None = None
    timeout_seconds: int | None = None
    limit: int | None = None


class AgentSettingsInput(BaseModel):
    option_thesis: AgentCommandSettingsInput | None = None
    option_postmortem: AgentCommandSettingsInput | None = None


class TradeJournalInput(BaseModel):
    ticker: str
    contract_id: str
    event_id: str | None = None
    strategy_version: str = DEFAULT_STRATEGY_VERSION
    opportunity: dict[str, Any] = {}
    notes: str = ""


def _context(cache_key: str = "full", loader: Callable[[dict[str, Any]], Any] | None = None) -> tuple[dict[str, Any], Any]:
    with _CONTEXT_LOCK:
        config = load_config()
        config_key = str(database_path(config))
        now = time.monotonic()
        entries = _CONTEXT_CACHE.setdefault("entries", {})
        cached = entries.get(cache_key)
        if cached is not None and cached.get("config_key") == config_key and now < float(cached.get("expires_at") or 0):
            return cached["value"]
        active_loader = loader or load_panel_data
        value = (config, active_loader(config))
        entries[cache_key] = {"value": value, "config_key": config_key, "expires_at": now + CONTEXT_CACHE_TTL_SECONDS}
        if cache_key == "full":
            _CONTEXT_CACHE.update({"value": value, "config_key": config_key, "expires_at": now + CONTEXT_CACHE_TTL_SECONDS})
        return value


def _table_payload(table_name: str) -> dict[str, Any]:
    _, panel_data = _context(cache_key=f"table:{table_name}", loader=lambda config: load_table_panel_data(config, table_name))
    return table_payload(panel_data, table_name)


def _capped_table_payload(table_name: str, limit: int) -> dict[str, Any]:
    payload = _table_payload(table_name)
    rows = payload["rows"]
    safe_limit = max(1, min(int(limit or SOURCE_FRESHNESS_DEFAULT_LIMIT), 500))
    capped_rows = rows[:safe_limit]
    return {
        **payload,
        "rows": capped_rows,
        "count": len(rows),
        "returned_count": len(capped_rows),
        "limit": safe_limit,
    }


def _invalidate_context_cache() -> None:
    _CONTEXT_CACHE.update({"entries": {}, "expires_at": 0.0, "config_key": None, "value": None})


def _execute_background_refresh_job(job_id: str, job_name: str, db_path: Path) -> None:
    try:
        execute_refresh_job(job_id, job_name, db_path, "config.yaml", raise_on_error=False)
    finally:
        _invalidate_context_cache()


def _payload_strategy_version(payload: dict[str, Any]) -> str:
    request = payload.get("request")
    request_strategy = request.get("strategy_version") if isinstance(request, dict) else None
    return str(payload.get("strategy_version") or request_strategy or DEFAULT_STRATEGY_VERSION)


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
