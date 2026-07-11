"""Shared app service layer for the FastAPI routers.

Routers import this module as `deps` and reference every app-service name as
`deps.X` (helpers, loaders, db, domain functions, models, constants). This keeps
a single seam: tests patch `app.deps.<name>` and every route reader resolves
through this namespace. Import from here; do not re-grow `app/main.py` with route
logic — add a router under `app/routers/` instead.
"""
from __future__ import annotations

import json
import inspect
import time
from copy import deepcopy
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
    portfolio_rows,
    options_radar_rows,
    populate_watchlist_symbol_data,
    delete_portfolio_position,
    delete_watchlist_symbol,
    mark_thesis_reviewed,
    save_portfolio_position,
    save_thesis,
    save_watchlist_symbol,
    settings_payload,
    signals_payload,
    status_payload,
    table_payload,
    thesis_monitor_rows,
    thesis_rows,
    ticker_payload,
    update_agent_settings_config,
    update_research_sources_config,
    user_state_table_payload,
    watchlist_rows,
)
from investment_panel.core.refresh_jobs import (
    ALLOWLIST,
    execute_refresh_job,
    execute_refresh_job_subprocess,
    refresh_job_rows,
    run_refresh_job,
    start_refresh_job,
)
from investment_panel.core.brokers import build_and_persist_agent_recommendations, stage_paper_order
from investment_panel.core.config import config_to_dict, load_config as load_core_config
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.option_agent_postmortem import AgentPostmortemValidationError, upsert_agent_postmortem
from investment_panel.core.option_agent_thesis import AgentThesisValidationError, build_ondemand_agent_request, refresh_option_agent_work, upsert_agent_thesis
from investment_panel.core.options_radar import (
    DEFAULT_STRATEGY_VERSION,
    StrategyPromotionError,
    acknowledge_radar_alert,
    promote_strategy_mutation,
    record_trade_journal_entry,
    refresh_strategy_proposal_evaluations,
)
from investment_panel.core.panel import build_source_catalog_health
from investment_panel.core.sources import source_detail_payload, source_ingestion_audit


APP_TITLE = "Personal Investment Panel"
CONTEXT_CACHE_TTL_SECONDS = 3.0
SOURCE_FRESHNESS_DEFAULT_LIMIT = 100
TAILSCALE_CGNAT = ip_network("100.64.0.0/10")
_CONTEXT_CACHE: dict[str, Any] = {"entries": {}, "expires_at": 0.0, "config_key": None, "value": None}
_CONTEXT_LOCK = RLock()
_LAST_GOOD_SCOPE_SNAPSHOTS: dict[str, dict[str, Any]] = {}
_SCOPE_SNAPSHOT_FALLBACK_TABLES = {
    "options-radar": {"option_radar_summary", "option_radar_opportunity", "candidate_event", "radar_alert"},
}


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


class ThesisInput(BaseModel):
    thesis: str
    why: str = ""
    invalidation: str = ""
    invalidation_price: float | None = None
    status: str | None = None
    evidence_links: list[str] | None = None


class PaperOrderInput(BaseModel):
    recommendation_id: str


class StrategyPromotionInput(BaseModel):
    approved_by: str = "joe"


class AgentCommandSettingsInput(BaseModel):
    enabled: bool | None = None
    command: str | None = None
    timeout_seconds: int | None = None
    limit: int | None = None


class OptionAgentSettingsInput(BaseModel):
    enabled: bool | None = None
    command: str | None = None
    timeout_seconds: int | None = None
    thesis_limit: int | None = None
    postmortem_limit: int | None = None
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    auto_run_seconds: int | None = None
    max_runs_per_day: int | None = None
    context_sources: dict[str, bool] | None = None


class AgentSettingsInput(BaseModel):
    option_thesis: AgentCommandSettingsInput | None = None
    option_postmortem: AgentCommandSettingsInput | None = None
    option_agent: OptionAgentSettingsInput | None = None


class ResearchXSettingsInput(BaseModel):
    enabled: bool | None = None
    list_id: str | None = None
    priority_handles: list[str] | str | None = None
    limit: int | None = None
    account_fetch_cap: int | None = None


class ResearchNewsSettingsInput(BaseModel):
    enabled: bool | None = None
    providers: list[str] | str | None = None
    limit: int | None = None


class ResearchBlogsSettingsInput(BaseModel):
    enabled: bool | None = None
    substack_urls: list[str] | str | None = None
    rss_urls: list[str] | str | None = None


class ResearchSourcesInput(BaseModel):
    x: ResearchXSettingsInput | None = None
    news: ResearchNewsSettingsInput | None = None
    blogs: ResearchBlogsSettingsInput | None = None


class AgentAnalyzeInput(BaseModel):
    ticker: str
    prompt: str | None = None


class TradeJournalInput(BaseModel):
    ticker: str
    contract_id: str
    event_id: str | None = None
    strategy_version: str = DEFAULT_STRATEGY_VERSION
    opportunity: dict[str, Any] = {}
    notes: str = ""


def _context(cache_key: str = "full", loader: Callable[[dict[str, Any]], Any] | None = None) -> tuple[dict[str, Any], Any]:
    config = load_config()
    config_key = str(database_path(config))
    now = time.monotonic()
    with _CONTEXT_LOCK:
        entries = _CONTEXT_CACHE.setdefault("entries", {})
        cached = entries.get(cache_key)
        if cached is not None and cached.get("config_key") == config_key and now < float(cached.get("expires_at") or 0):
            return cached["value"]

    active_loader = loader or _load_panel_data_without_repairs
    value = (config, active_loader(config))

    with _CONTEXT_LOCK:
        entries = _CONTEXT_CACHE.setdefault("entries", {})
        entries[cache_key] = {"value": value, "config_key": config_key, "expires_at": now + CONTEXT_CACHE_TTL_SECONDS}
        if cache_key == "full":
            _CONTEXT_CACHE.update({"value": value, "config_key": config_key, "expires_at": now + CONTEXT_CACHE_TTL_SECONDS})
        return value


def _load_panel_data_without_repairs(active_config: dict[str, Any]) -> Any:
    parameters = inspect.signature(load_panel_data).parameters
    if "ensure_decision_models" not in parameters:
        return load_panel_data(active_config)
    return load_panel_data(
        active_config,
        ensure_decision_models=False,
        ensure_source_models=False,
    )


def _table_payload(table_name: str) -> dict[str, Any]:
    _, panel_data = _context(cache_key=f"table:{table_name}", loader=lambda config: load_table_panel_data(config, table_name))
    return table_payload(panel_data, table_name)


def scope_panel_snapshot_payload(
    config: dict[str, Any],
    panel_data: Any,
    scope: str,
    *,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    payload = panel_snapshot_payload(panel_data, scope, offset=offset, limit=limit)
    if scope not in _SCOPE_SNAPSHOT_FALLBACK_TABLES or offset != 0 or limit is not None:
        return payload
    if _scope_snapshot_has_rows(scope, payload):
        _store_last_good_scope_snapshot(config, scope, payload)
        return payload
    fallback = _load_last_good_scope_snapshot(config, scope)
    if fallback is None:
        return payload
    status = dict(fallback.get("status") or {})
    status.update(
        {
            "ready": True,
            "source": "panel-snapshot-cache",
            "message": "Serving last good options-radar snapshot while the live DuckDB read is unavailable.",
        }
    )
    fallback["status"] = status
    return fallback


def _scope_snapshot_has_rows(scope: str, payload: dict[str, Any]) -> bool:
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        return False
    for table_name in _SCOPE_SNAPSHOT_FALLBACK_TABLES.get(scope, set()):
        table = tables.get(table_name)
        rows = table.get("rows") if isinstance(table, dict) else None
        if isinstance(rows, list) and rows:
            return True
    return False


def _store_last_good_scope_snapshot(config: dict[str, Any], scope: str, payload: dict[str, Any]) -> None:
    snapshot = deepcopy(payload)
    _LAST_GOOD_SCOPE_SNAPSHOTS[scope] = snapshot
    path = _scope_snapshot_cache_path(config, scope)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_text(json.dumps(snapshot, ensure_ascii=False, default=str), encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        return


def _load_last_good_scope_snapshot(config: dict[str, Any], scope: str) -> dict[str, Any] | None:
    cached = _LAST_GOOD_SCOPE_SNAPSHOTS.get(scope)
    if cached is not None:
        return deepcopy(cached)
    path = _scope_snapshot_cache_path(config, scope)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or not _scope_snapshot_has_rows(scope, payload):
        return None
    _LAST_GOOD_SCOPE_SNAPSHOTS[scope] = payload
    return deepcopy(payload)


def _scope_snapshot_cache_path(config: dict[str, Any], scope: str) -> Path:
    db_path = database_path(config)
    return db_path.parent / "api-cache" / f"panel-snapshot-{scope}.json"


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
        execute_refresh_job_subprocess(job_id, job_name, db_path, "config.yaml")
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
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return _with_data_freshness(payload)


# Housekeeping steps run after data ingestion and don't affect data freshness;
# mirror the orchestrator so a snapshot/prune failure never hides fresh data.
_HOUSEKEEPING_REFRESH_STEPS = frozenset({"retention_prune", "database_snapshot"})


def _with_data_freshness(payload: dict[str, Any]) -> dict[str, Any]:
    """Backfill dataOk/dataFinishedAt for status files written before the split."""

    if not isinstance(payload, dict):
        return payload
    if payload.get("dataOk") is not None:
        return payload
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        return payload
    data_ok = all(
        step.get("ok")
        for step in steps
        if isinstance(step, dict) and step.get("name") not in _HOUSEKEEPING_REFRESH_STEPS
    )
    payload["dataOk"] = data_ok
    payload["dataFinishedAt"] = payload.get("finishedAt") if data_ok else None
    return payload


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
