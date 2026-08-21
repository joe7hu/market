"""Canonical panel read-model and health routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app import panel_snapshot as panel_owner
from app.actions.options import OptionsActions
from app.data_access import config as config_owner
from app.data_access import loaders, payloads

router = APIRouter()


@router.get("/api/status")
def status() -> dict[str, Any]:
    config = config_owner.load_config()
    panel_data = loaders.load_panel_data(
        config,
        table_names=("source_health",),
        ensure_decision_models=False,
        ensure_source_models=False,
    )
    response = payloads.status_payload(panel_data)
    try:
        response["options_history"] = OptionsActions(config).history_health()
    except Exception as exc:  # status must stay available during a migration outage
        response["options_history"] = {"available": False, "message": f"{type(exc).__name__}: {exc}"}
    return response


@router.get("/api/panel-contract")
def panel_contract() -> dict[str, Any]:
    return loaders.panel_contract_payload()


@router.get("/api/panel-snapshot")
def panel_snapshot(scope: str = "dashboard", offset: int = 0, limit: int | None = None) -> dict[str, Any]:
    if scope == "market":
        config = config_owner.load_config()
        panel_data = loaders.load_market_panel_data(config)
        return panel_owner.scope_snapshot_payload(config, panel_data, scope, offset=offset, limit=limit)
    if scope == "dashboard":
        _, panel_data = panel_owner.context()
        return payloads.panel_snapshot_payload(panel_data, scope, offset=offset, limit=limit)
    if scope in {"watchlist-watched", "watchlist-unwatched"}:
        config, panel_data = panel_owner.context(
            cache_key=f"scope:{scope}:{offset}:{limit}",
            loader=lambda active_config: loaders.load_watchlist_scope_data(active_config, scope, offset=offset, limit=limit),
        )
        return panel_owner.scope_snapshot_payload(config, panel_data, scope, offset=offset, limit=limit)
    if scope == "research":
        config, panel_data = panel_owner.context(
            cache_key="scope:research",
            loader=loaders.load_daily_research_panel_data,
        )
        return panel_owner.scope_snapshot_payload(config, panel_data, scope, offset=offset, limit=limit)
    config, panel_data = panel_owner.context(
        cache_key=f"scope:{scope}",
        loader=lambda active_config: loaders.load_panel_scope_data(active_config, scope),
    )
    return panel_owner.scope_snapshot_payload(config, panel_data, scope, offset=offset, limit=limit)


__all__ = ["router"]
