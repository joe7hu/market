"""Canonical panel read-model and health routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends

from app import panel_snapshot as panel_owner
from app import dependencies
from app.actions.options import OptionsActions
from app.data_access import loaders, payloads
from app.response_contracts import PanelContractResponse, PanelSnapshotResponse, StatusResponse, TodayResponse
from investment_panel.core.config import AppConfig

router = APIRouter()


@router.get("/api/today", response_model=TodayResponse, response_model_exclude_unset=True)
def today(
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    """Return exact ticker capital actions, ordered by action priority."""

    _, panel_data = panel_owner.context(
        cache_key="scope:today",
        loader=lambda active_config: loaders.load_panel_scope_data(active_config, "today"),
        config_loader=lambda: config,
    )
    symbols: list[str] = []
    for name in ("ticker_decisions", "portfolio"):
        for row in panel_data.rows(name):
            symbol = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    actions: list[dict[str, Any]] = []
    for symbol in symbols[:100]:
        ticker_panel = loaders.load_ticker_panel_data(config, symbol)
        decision = payloads.ticker_payload(ticker_panel, symbol)["ticker_decision"]
        capital = dict(decision["capital_action"])
        selected = decision.get("selected_expression") or {}
        actions.append({
            **capital,
            "ticker": symbol,
            "decision_revision": decision["decision_revision"],
            "selected_expression": selected.get("kind"),
        })
    priority = {"EXIT": 0, "TRIM": 1, "HEDGE": 2, "BUY": 3, "ADD": 4, "WAIT_FOR_PRICE": 5, "HOLD": 6, "AVOID": 7}
    actions.sort(key=lambda row: (priority.get(str(row.get("action")), 99), str(row.get("ticker"))))
    timestamps = [
        timestamp
        for name in ("ticker_decisions", "portfolio")
        if (timestamp := _latest_timestamp(panel_data.rows(name))) is not None
    ]
    as_of = max(timestamps, default=None)
    return {
        "status": payloads.status_payload(panel_data),
        "as_of": as_of,
        "actions": actions,
        "count": len(actions),
    }


def _latest_timestamp(rows: list[dict[str, Any]]) -> datetime | None:
    values: list[datetime] = []
    for row in rows:
        value = row.get("available_at") or row.get("as_of")
        if isinstance(value, datetime):
            values.append(value if value.tzinfo else value.replace(tzinfo=UTC))
            continue
        if value:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            values.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC))
    return max(values, default=None)


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
