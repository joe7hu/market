"""Canonical panel read-model and health routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app import panel_snapshot as panel_owner
from app import dependencies
from app.actions.options import OptionsActions
from app.data_access import loaders, payloads
from app.response_contracts import PanelContractResponse, PanelSnapshotResponse, StatusResponse
from investment_panel.core.config import AppConfig

router = APIRouter()


@router.get("/api/status", response_model=StatusResponse, response_model_exclude_unset=True)
def status(
    config: AppConfig = Depends(dependencies.get_config),
    actions: OptionsActions = Depends(dependencies.get_options_actions),
) -> dict[str, Any]:
    panel_data = loaders.load_panel_data(
        config,
        table_names=("source_health",),
        ensure_decision_models=False,
        ensure_source_models=False,
    )
    response = payloads.status_payload(panel_data)
    try:
        response["options_history"] = actions.history_health()
    except Exception as exc:  # status must stay available during a migration outage
        response["options_history"] = {"available": False, "message": f"{type(exc).__name__}: {exc}"}
    return response


@router.get("/api/panel-contract", response_model=PanelContractResponse, response_model_exclude_unset=True)
def panel_contract() -> dict[str, Any]:
    return loaders.panel_contract_payload()


@router.get("/api/panel-snapshot", response_model=PanelSnapshotResponse, response_model_exclude_unset=True)
def panel_snapshot(
    scope: str = "dashboard",
    offset: int = 0,
    limit: int | None = None,
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    if scope == "market":
        panel_data = loaders.load_market_panel_data(config)
        return panel_owner.scope_snapshot_payload(config, panel_data, scope, offset=offset, limit=limit)
    if scope == "dashboard":
        _, panel_data = panel_owner.context(config_loader=lambda: config)
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
            cache_key="scope:research",
            loader=loaders.load_daily_research_panel_data,
            config_loader=lambda: config,
        )
        return panel_owner.scope_snapshot_payload(config, panel_data, scope, offset=offset, limit=limit)
    config, panel_data = panel_owner.context(
        cache_key=f"scope:{scope}",
        loader=lambda active_config: loaders.load_panel_scope_data(active_config, scope),
        config_loader=lambda: config,
    )
    return panel_owner.scope_snapshot_payload(config, panel_data, scope, offset=offset, limit=limit)


__all__ = ["router"]
