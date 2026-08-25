"""Canonical panel read-model and health routes."""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from typing import Any

from fastapi import APIRouter, Depends

from app import panel_snapshot as panel_owner
from app import dependencies
from app.actions.options import OptionsActions
from app.data_access import loaders, payloads
from app.response_contracts import PanelContractResponse, PanelSnapshotResponse, StatusResponse, TodayResponse
from investment_panel.core.config import AppConfig
from investment_panel.core.decision import (
    build_decision_resolution,
    capital_action_from_resolution,
    resolution_from_legacy,
    trade_expression_identity,
)

router = APIRouter()


@router.get("/api/today", response_model=TodayResponse, response_model_exclude_unset=True)
def today(
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    """Return exact ticker capital actions, ordered by backend opportunity rank."""

    _, panel_data = panel_owner.context(
        cache_key="scope:today",
        loader=lambda active_config: loaders.load_panel_scope_data(active_config, "today"),
        config_loader=lambda: config,
    )
    actions: list[dict[str, Any]] = []
    rank_rows = panel_data.rows("opportunity_rank")
    # The published ticker row already contains the deterministic capital
    # action. Do not reload a full dossier for every symbol: that makes this
    # summary route depend on deep evidence and option-surface queries.
    for row in panel_data.rows("ticker_decisions"):
        symbol = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        rank = _rank_for_row(row, rank_rows, symbol)
        rank_ready, rank_reason = _rank_ready(row, rank)
        if rank_ready:
            resolution = resolution_from_legacy({**row, "ticker": symbol})
        else:
            revision = str(row.get("decision_revision") or "legacy")
            policy_version = str(row.get("policy_version") or "risk-policy.v2:legacy")
            original_capital = row.get("capital_action") if isinstance(row.get("capital_action"), dict) else {}
            resolution = build_decision_resolution(
                action="NO_TRADE",
                decision_revision=revision,
                policy_version=policy_version,
                provenance={"as_of": row.get("as_of"), "input_hash": row.get("input_hash")},
                ticker=symbol,
                blockers=[rank_reason],
                data_quality="INCOMPLETE",
                authorization_mode="NONE",
                rationale=f"Cash is selected because the current opportunity rank is unavailable: {rank_reason}.",
                owned=bool(original_capital.get("owned")),
                expires_at=original_capital.get("expires_at"),
                blocked=True,
            )
        capital_value = capital_action_from_resolution(resolution).model_dump(mode="json")
        if not symbol or not isinstance(capital_value, dict) or not capital_value.get("action"):
            continue
        capital = dict(capital_value)
        selected = row.get("selected_expression")
        selected = selected if isinstance(selected, dict) else {}
        actions.append({
            **capital,
            "ticker": symbol,
            "decision_revision": row.get("decision_revision") or "",
            "selected_expression": selected.get("kind") if rank_ready else "CASH",
            "research_rank": rank.get("research_rank") if rank else None,
            "trade_rank": rank.get("trade_rank") if rank_ready and rank else None,
            "trade_rank_unavailable_reason": None if rank_ready else rank_reason,
            "trade_utility": rank.get("trade_utility") if rank_ready and rank else None,
        })
    actions.sort(key=lambda row: (
        0 if row.get("trade_rank") is not None else 1,
        int(row.get("trade_rank") or 0) if row.get("trade_rank") is not None else 0,
        0 if row.get("research_rank") is not None else 1,
        int(row.get("research_rank") or 0) if row.get("research_rank") is not None else 0,
        str(row.get("ticker")),
    ))
    actions = actions[:100]
    timestamps = [
        timestamp
        for name in ("ticker_decisions",)
        if (timestamp := _latest_timestamp(panel_data.rows(name))) is not None
    ]
    as_of = max(timestamps, default=None)
    return {
        "status": payloads.status_payload(panel_data),
        "as_of": as_of,
        "actions": actions,
        "count": len(actions),
    }


def _rank_for_row(row: dict[str, Any], ranks: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    revision = str(row.get("decision_revision") or "")
    episode_id = str(row.get("opportunity_episode_id") or "")
    matches = [
        rank for rank in ranks
        if str(rank.get("ticker") or rank.get("symbol") or "").upper() == symbol
        and str(rank.get("decision_revision") or "") == revision
        and str(rank.get("opportunity_episode_id") or "") == episode_id
    ]
    return matches[0] if len(matches) == 1 else None


def _rank_ready(row: dict[str, Any], rank: dict[str, Any] | None) -> tuple[bool, str]:
    if rank is None:
        return False, "opportunity_rank_missing"
    selected = row.get("selected_expression") if isinstance(row.get("selected_expression"), dict) else {}
    if str(rank.get("selected_expression_kind") or "") != str(selected.get("kind") or ""):
        return False, "opportunity_rank_identity_mismatch"
    try:
        if str(rank.get("selected_expression_identity") or "") != trade_expression_identity(selected):
            return False, "opportunity_rank_identity_mismatch"
        if not bool(rank.get("evaluated_universe_complete")):
            return False, "ranking_universe_incomplete"
        rank_utility = float(rank.get("trade_utility"))
        if int(rank.get("trade_rank")) <= 0 or not isfinite(rank_utility) or rank_utility <= 0:
            return False, str(rank.get("trade_rank_unavailable_reason") or "opportunity_rank_unavailable")
    except (TypeError, ValueError, OverflowError):
        return False, str(rank.get("trade_rank_unavailable_reason") or "opportunity_rank_unavailable")
    return not bool(rank.get("trade_rank_unavailable_reason")), ""


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
