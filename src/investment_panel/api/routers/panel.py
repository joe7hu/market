"""Canonical panel read-model and health routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from investment_panel.api import panel_snapshot as panel_owner
from investment_panel.api import dependencies
from investment_panel.workflows import today as today_actions
from investment_panel.api.data_access import loaders, payloads
from investment_panel.api.response_contracts import PanelContractResponse, PanelSnapshotResponse, StatusResponse, TodayResponse
from investment_panel.settings import AppConfig
from investment_panel.domain.panel import tables_for_scope

router = APIRouter()
ACTION_QUEUE_LIMIT = 10


@router.get("/api/today", response_model=TodayResponse, response_model_exclude_unset=True)
def today(
    config: AppConfig = Depends(dependencies.get_config),
    option_actions: dependencies.OptionsResearchRepository = Depends(dependencies.get_options_research),
) -> dict[str, Any]:
    """Return one bounded, source-ordered action queue."""
    return today_actions.today(config, option_actions)


@router.get("/api/status", response_model=StatusResponse, response_model_exclude_unset=True)
def status(
    config: AppConfig = Depends(dependencies.get_config),
    actions: dependencies.OptionsHistoryService = Depends(dependencies.get_options_history),
) -> dict[str, Any]:
    panel_data = loaders.load_panel_data(
        config,
        table_names=("source_health",),
    )
    response = payloads.status_payload(panel_data)
    try:
        response["options_history"] = actions.health(mode=config.analysis.options_decision_system.mode)
    except Exception as exc:  # status must stay available during a migration outage
        response["options_history"] = {"available": False, "message": f"{type(exc).__name__}: {exc}"}
    return response



@router.get("/api/panel-contract", response_model=PanelContractResponse, response_model_exclude_unset=True)
def panel_contract() -> dict[str, Any]:
    return loaders.panel_contract_payload()


@router.get("/api/panel-snapshot", response_model=PanelSnapshotResponse, response_model_exclude_unset=True)
def panel_snapshot(
    scope: str = "dashboard",
    offset: int = Query(0, ge=0, le=10_000),
    limit: int | None = Query(None, ge=1, le=500),
    include_screener: bool = False,
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    try:
        tables_for_scope(scope)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if scope == "market":
        panel_data = loaders.load_market_panel_data(config, offset=offset, limit=limit)
        return panel_owner.scope_snapshot_payload(config, panel_data, scope, offset=offset, limit=limit)
    if scope == "dashboard":
        dashboard_limit = limit or 12
        _, panel_data = panel_owner.context(
            cache_key=f"scope:dashboard:{offset}:{dashboard_limit}",
            loader=lambda active_config: loaders.load_panel_scope_data(
                active_config,
                "dashboard",
                offset=offset,
                limit=dashboard_limit,
            ),
            config_loader=lambda: config,
        )
        return payloads.panel_snapshot_payload(panel_data, scope, offset=offset, limit=limit)
    if scope in {"watchlist-watched", "watchlist-unwatched"}:
        config, panel_data = panel_owner.context(
            cache_key=f"scope:{scope}:{offset}:{limit}",
            loader=lambda active_config: loaders.load_watchlist_scope_data(active_config, scope, offset=offset, limit=limit),
            config_loader=lambda: config,
        )
        return panel_owner.scope_snapshot_payload(config, panel_data, scope, offset=offset, limit=limit)
    if scope == "research":
        config, panel_data = panel_owner.context(
            cache_key=f"scope:research:{offset}:{limit}",
            loader=lambda active_config: loaders.load_daily_research_panel_data(
                active_config,
                offset=offset,
                limit=limit,
            ),
            config_loader=lambda: config,
        )
        return panel_owner.scope_snapshot_payload(config, panel_data, scope, offset=offset, limit=limit)
    cache_key = (
        "scope:today"
        if scope == "today" and offset == 0 and limit is None
        else f"scope:{scope}:{offset}:{limit}:{include_screener}"
    )
    if scope == "today":
        config, panel_data = panel_owner.context(
            cache_key=cache_key,
            loader=lambda active_config: loaders.load_panel_scope_data(
                active_config,
                scope,
                offset=offset,
                limit=limit,
                include_screener=include_screener,
            ),
            config_loader=lambda: config,
        )
        return panel_owner.scope_snapshot_payload(config, panel_data, scope, offset=offset, limit=limit)

    def load_snapshot(active_config: AppConfig) -> dict[str, Any]:
        panel_data = loaders.load_panel_scope_data(
            active_config,
            scope,
            offset=offset,
            limit=limit,
            include_screener=include_screener,
        )
        return panel_owner.scope_snapshot_payload(
            active_config,
            panel_data,
            scope,
            offset=offset,
            limit=limit,
        )

    _, snapshot_payload = panel_owner.context(
        cache_key=cache_key,
        loader=load_snapshot,
        config_loader=lambda: config,
    )
    return snapshot_payload


__all__ = ["router"]
