"""Panel / dashboard / decision read-model routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app import deps

router = APIRouter()


@router.get("/api/status")
def status() -> dict[str, Any]:
    config, panel_data = deps._context()
    return deps.settings_payload(config, panel_data)["status"]


@router.get("/api/panel-contract")
def panel_contract() -> dict[str, Any]:
    return deps.panel_contract_payload()


@router.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    _, panel_data = deps._context()
    return deps.dashboard_payload(panel_data)


@router.get("/api/panel-snapshot")
def panel_snapshot(scope: str = "dashboard", offset: int = 0, limit: int | None = None) -> dict[str, Any]:
    if scope == "market":
        config = deps.load_config()
        return deps.panel_snapshot_payload(deps.load_market_panel_data(config), scope, offset=offset, limit=limit)
    if scope == "dashboard":
        _, panel_data = deps._context()
        return deps.panel_snapshot_payload(panel_data, scope, offset=offset, limit=limit)
    _, panel_data = deps._context(cache_key=f"scope:{scope}", loader=lambda config: deps.load_panel_scope_data(config, scope))
    return deps.panel_snapshot_payload(panel_data, scope, offset=offset, limit=limit)


@router.get("/api/decision-readiness")
def decision_readiness() -> dict[str, Any]:
    return deps._table_payload("decision_readiness")


@router.get("/api/candidates")
def candidates() -> dict[str, Any]:
    return deps._table_payload("candidates")


@router.get("/api/signals")
def signals() -> dict[str, Any]:
    _, panel_data = deps._context(
        cache_key="table:signals",
        loader=lambda config: deps.load_panel_data(config, table_names=("signals", "candidates")),
    )
    return deps.signals_payload(panel_data)


@router.get("/api/opportunities-ranked")
def opportunities_ranked() -> dict[str, Any]:
    return deps._table_payload("opportunities_ranked")


@router.get("/api/opportunity-sources")
def opportunity_sources() -> dict[str, Any]:
    return deps._table_payload("opportunity_sources")


@router.get("/api/discovered-universe")
def discovered_universe() -> dict[str, Any]:
    return deps._table_payload("discovered_universe")


@router.get("/api/decision-queue")
def decision_queue() -> dict[str, Any]:
    return deps._table_payload("decision_queue")


@router.get("/api/source-freshness")
def source_freshness(limit: int = deps.SOURCE_FRESHNESS_DEFAULT_LIMIT) -> dict[str, Any]:
    return deps._capped_table_payload("source_freshness", limit=limit)


@router.get("/api/symbol-decision-snapshots")
def symbol_decision_snapshots() -> dict[str, Any]:
    return deps._table_payload("symbol_decision_snapshots")


@router.get("/api/market-context")
def market_context() -> dict[str, Any]:
    return deps._table_payload("market_context")


@router.get("/api/daily-brief")
def daily_brief() -> dict[str, Any]:
    return deps._table_payload("daily_brief")


@router.get("/api/feed")
def feed() -> dict[str, Any]:
    return deps._table_payload("feed_signals")
