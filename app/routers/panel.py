"""Canonical panel read-model and health routes."""

from __future__ import annotations

from datetime import UTC, date, datetime
from math import isfinite
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query

from app import panel_snapshot as panel_owner
from app import dependencies
from app.actions.options import OptionsActions
from app.data_access import loaders, payloads
from app.response_contracts import PanelContractResponse, PanelSnapshotResponse, StatusResponse, TodayResponse
from investment_panel.core.config import AppConfig
from investment_panel.core.panel import tables_for_scope
from investment_panel.core.decision import (
    build_decision_resolution,
    capital_action_from_resolution,
    resolution_from_legacy,
    trade_expression_identity,
)

router = APIRouter()
ACTION_QUEUE_LIMIT = 100


@router.get("/api/today", response_model=TodayResponse, response_model_exclude_unset=True)
def today(
    config: AppConfig = Depends(dependencies.get_config),
    option_actions: OptionsActions = Depends(dependencies.get_options_actions),
) -> dict[str, Any]:
    """Return one bounded, source-ordered action queue."""

    _, panel_data = panel_owner.context(
        cache_key="scope:today",
        loader=lambda active_config: loaders.load_panel_scope_data(active_config, "today"),
        config_loader=lambda: config,
    )
    capital_actions: list[dict[str, Any]] = []
    rank_rows = panel_data.rows("opportunity_rank")
    plan_rows = panel_data.rows("trade_plan")
    # The published ticker row already contains the deterministic capital
    # action. Do not reload a full dossier for every symbol: that makes this
    # summary route depend on deep evidence and option-surface queries.
    for row in panel_data.rows("ticker_decisions"):
        symbol = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        rank = loaders.today_rank_for_row(row, rank_rows, symbol)
        plan = loaders.today_plan_for_row(row, plan_rows, rank, symbol)
        rank_ready, rank_reason = _rank_ready(row, rank)
        if plan is not None and (plan.eligibility == "BLOCKED" or rank_ready):
            try:
                resolution = resolution_from_legacy({**row, "ticker": symbol})
                if resolution.trade_plan_id != plan.trade_plan_id:
                    raise ValueError("trade plan resolution identity mismatch")
            except (TypeError, ValueError, KeyError):
                plan = None
                rank_reason = "trade_plan_identity_mismatch"
                resolution = None
        else:
            rank_reason = "trade_plan_missing" if plan is None else rank_reason
            plan = None
            resolution = None
        if plan is None:
            if not rank_reason:
                rank_reason = "trade_plan_missing"
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
                rationale=f"Cash is selected because the current trade plan is unavailable: {rank_reason}.",
                owned=bool(original_capital.get("owned")),
                expires_at=original_capital.get("expires_at"),
                blocked=True,
            )
        else:
            rank_reason = plan.primary_blocker or ""
        capital_value = capital_action_from_resolution(resolution).model_dump(mode="json")
        if not isinstance(capital_value, dict) or not capital_value.get("action"):
            continue
        capital = dict(capital_value)
        revision = str(row.get("decision_revision") or "").strip()
        decision_id = str(row.get("ticker_decision_id") or "").strip()
        identity = decision_id or (f"{symbol}:{revision}" if symbol and revision else "")
        identity_missing = not symbol or not identity
        blocked = identity_missing or plan is None or plan.eligibility == "BLOCKED"
        if blocked:
            # ``capital_action_from_resolution`` retains the old AVOID label
            # for compatibility. The queue must expose its fail-closed state.
            capital["action"] = "NO_TRADE"
        authority = identity or "missing"
        capital_actions.append({
            **capital,
            "projection_identity": f"capital:ticker-decision:{authority}",
            "source_authority": f"ticker-decision:{authority}",
            "source": "capital_action",
            "title": f"{symbol} capital action" if symbol else "Unavailable ticker decision",
            "lifecycle_state": "unavailable" if identity_missing else "blocked" if blocked else "actionable",
            "transition": None,
            "current_at": _queue_datetime(row.get("published_at") or row.get("available_at") or row.get("as_of")),
            "primary_blocker": "ticker_decision_identity_missing" if identity_missing else (rank_reason or "trade_plan_blocked") if blocked else None,
            "next_action": plan.next_action if plan is not None else "Refresh the ticker decision and trade plan.",
            "drill_down": f"/tickers/{quote(symbol)}" if symbol else None,
            "ticker": symbol,
            "decision_revision": revision or None,
            "policy_version": resolution.policy_version,
            "resolution": _today_resolution_payload(resolution),
            "selected_expression": plan.selected_expression_kind.value if plan is not None else "CASH",
            "research_rank": (
                rank.get("research_rank")
                if rank and _has_positive_research_rank(rank.get("research_rank"))
                else None
            ),
            "trade_rank": rank.get("trade_rank") if plan is not None and rank_ready and rank else None,
            "trade_rank_unavailable_reason": None if plan is not None and rank_ready else rank_reason,
            "trade_utility": rank.get("trade_utility") if plan is not None and rank_ready and rank else None,
            "trade_plan": _today_trade_plan_payload(plan) if plan is not None else None,
        })
    capital_actions.sort(key=lambda row: (
        0 if row.get("trade_rank") is not None else 1,
        int(row.get("trade_rank") or 0) if row.get("trade_rank") is not None else 0,
        0 if row.get("research_rank") is not None else 1,
        int(row.get("research_rank") or 0) if row.get("research_rank") is not None else 0,
        str(row.get("ticker")),
    ))
    sampled_missing_plan_count = sum(_is_unranked_missing_plan_action(row) for row in capital_actions)
    exact_missing_plan_count = panel_data.metadata.get("today_missing_plan_count")
    missing_plan_count = max(sampled_missing_plan_count, exact_missing_plan_count) if (
        isinstance(exact_missing_plan_count, int)
        and not isinstance(exact_missing_plan_count, bool)
        and exact_missing_plan_count >= 0
    ) else sampled_missing_plan_count
    visible_capital_actions = [row for row in capital_actions if not _is_unranked_today_action(row)]
    queue_items = _bounded_today_queue(
        visible_capital_actions,
        decision_inbox_queue(_read_inbox(option_actions)),
        _portfolio_risk_queue(panel_data.rows("portfolio_risk_cards")),
        research_queue(panel_data.rows("feed_signals")),
    )
    timestamps = [item["current_at"] for item in queue_items if item.get("current_at") is not None]
    timestamps.extend(
        timestamp
        for name in ("ticker_decisions", "portfolio_risk_cards", "feed_signals")
        if (timestamp := _latest_timestamp(panel_data.rows(name))) is not None
    )
    as_of = max(timestamps, default=None)
    return {
        "status": payloads.status_payload(panel_data),
        "as_of": as_of,
        "actions": queue_items,
        "book_actions": book_action_queue(capital_actions),
        "missing_plan_count": missing_plan_count,
        "count": len(queue_items),
    }


def _today_resolution_payload(resolution: Any) -> dict[str, Any]:
    return resolution.model_dump(mode="json", include={
        "contract_version", "lifecycle", "eligibility", "authorization_mode", "data_quality",
        "action", "trade_plan_id", "primary_blocker", "blockers", "next_action", "policy_version",
        "decision_revision", "ticker", "rationale", "owned", "price_condition", "catalyst", "expires_at",
    })


def _today_trade_plan_payload(plan: Any) -> dict[str, Any]:
    return plan.model_dump(mode="json", include={
        "contract_version", "trade_plan_id", "publication_id", "ticker", "opportunity_episode_id",
        "decision_revision", "policy_version", "selected_expression_kind", "selected_expression_identity",
        "rank_id", "alpha_signal_id", "portfolio_impact_id", "market_snapshot_id",
        "market_state_publication_id", "action", "eligibility", "authorization_mode", "data_quality",
        "rationale", "primary_blocker", "blockers", "next_action",
    })


def book_action_queue(rows: list[dict[str, Any]], *, limit: int = ACTION_QUEUE_LIMIT) -> list[dict[str, Any]]:
    """Rank current opportunity actions against the explicit cash alternative."""

    if limit <= 0:
        return []

    opportunities = sorted(
        (row for row in rows if row.get("source") == "capital_action" and row.get("ticker")),
        key=lambda row: (
            0 if row.get("trade_rank") is not None else 1,
            int(row.get("trade_rank") or 0) if row.get("trade_rank") is not None else 0,
            0 if row.get("research_rank") is not None else 1,
            int(row.get("research_rank") or 0) if row.get("research_rank") is not None else 0,
            str(row.get("ticker")),
        ),
    )
    output = [dict(row) for row in opportunities[: max(0, limit - 1)]]
    output.append({
        "projection_identity": "capital:book:CASH",
        "source_authority": "book:CASH",
        "source": "cash",
        "title": "Cash",
        "lifecycle_state": "current",
        "transition": None,
        "current_at": None,
        "primary_blocker": None,
        "next_action": "Hold cash until a qualified opportunity is published.",
        "drill_down": "/today",
        "ticker": None,
        "action": "CASH",
        "owned": False,
        "rationale": "No capital action is authorized without a current qualified rank and plan.",
        "decision_revision": None,
        "policy_version": "risk-policy.v2:book",
        "selected_expression": "CASH",
        "price_condition": None,
        "catalyst": None,
        "expires_at": None,
        "research_rank": None,
        "trade_rank": None,
        "trade_rank_unavailable_reason": None,
        "trade_utility": 0.0,
        "trade_plan": None,
    })
    for rank, row in enumerate(output, start=1):
        row["book_rank"] = rank
    return output


def _read_inbox(option_actions: Any) -> list[dict[str, Any]]:
    reader = getattr(option_actions, "decision_inbox", None)
    if not callable(reader):
        return []
    try:
        payload = reader(limit=ACTION_QUEUE_LIMIT, cursor=None)
    except Exception:
        # Inbox is an additive read source. A failed read cannot authorize or
        # revive an action, and the canonical panel status remains available.
        return []
    items = payload.get("items") if isinstance(payload, dict) else []
    return [dict(item) for item in items or [] if isinstance(item, dict)]


def decision_inbox_queue(rows: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    reference = _queue_datetime(now) or datetime.now(UTC)
    output: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("status") or "").lower() != "active" or row.get("resolved_at"):
            continue
        created_at = _queue_datetime(row.get("created_at"))
        if created_at is not None and created_at > reference:
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        event_type = _queue_text(row.get("event_type"), "transition").lower()
        if event_type == "portfolio_critical":
            continue
        identifier = _queue_text(row.get("id"))
        opportunity = _queue_text(row.get("opportunity_id"))
        ticket_version = _queue_text(row.get("ticket_version"))
        if identifier:
            authority = f"decision-inbox:{identifier}"
            unavailable = False
        elif opportunity or ticket_version:
            authority = f"decision-inbox:{opportunity or 'global'}:{ticket_version or '-'}:{event_type}"
            unavailable = False
        else:
            authority = "decision-inbox:missing"
            unavailable = True
        expiry_value = _queue_value(row, payload, "expires_at", "expiry", "expires")
        expiry = _queue_datetime(expiry_value)
        expiry_invalid = expiry_value is not None and expiry is None
        expired = event_type == "expired" or (
            expiry is not None and expiry <= reference
        )
        ticker = _queue_text(_queue_value(row, payload, "ticker", "symbol")).upper() or None
        blocker = _queue_text(_queue_value(row, payload, "primary_blocker", "blocker")) or None
        lifecycle = "unavailable" if unavailable or expiry_invalid else "expired" if expired else "transition"
        if unavailable:
            blocker = "decision_inbox_identity_missing"
        elif expiry_invalid:
            blocker = "decision_inbox_expiry_invalid"
        output.append({
            "projection_identity": f"inbox:{authority}",
            "source_authority": authority,
            "source": "decision_inbox",
            "title": _queue_text(_queue_value(row, payload, "title"), f"{event_type.replace('_', ' ').title()} transition"),
            "lifecycle_state": lifecycle,
            "transition": event_type.upper(),
            "current_at": created_at or reference,
            "expires_at": expiry,
            "primary_blocker": blocker,
            "next_action": _queue_text(
                _queue_value(row, payload, "next_action", "next_required_action", "required_next_action"),
                "Review the immutable Decision Inbox transition.",
            ),
            "drill_down": f"/tickers/{quote(ticker)}" if ticker else "/options-radar",
            "ticker": ticker,
            "action": "NO_TRADE",
            "rationale": _queue_text(_queue_value(row, payload, "rationale", "reason", "summary")),
            "decision_revision": _queue_text(_queue_value(row, payload, "decision_revision", "revision")) or None,
            "policy_version": _queue_text(_queue_value(row, payload, "policy_version"), "risk-policy.v2:legacy"),
        })
    return output


def _portfolio_risk_queue(rows: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    reference = _queue_datetime(now) or datetime.now(UTC)
    output: list[dict[str, Any]] = []
    for row in rows:
        severity = _queue_text(row.get("severity"), "info").lower()
        if severity not in {"critical", "watch"}:
            continue
        card_id = _queue_text(row.get("card_id"))
        title = _queue_text(row.get("title"), "Portfolio risk exception")
        authority = f"portfolio-risk:{card_id}" if card_id else "portfolio-risk:missing"
        identity_missing = not card_id
        ticker = _queue_text(row.get("symbol"))
        blocker = _queue_text(row.get("primary_blocker", row.get("blocker"))) or None
        if identity_missing:
            blocker = "portfolio_risk_identity_missing"
        elif severity == "critical" and not blocker:
            blocker = _queue_text(row.get("risk_type"), "portfolio_risk_exception")
        output.append({
            "projection_identity": f"portfolio:{authority}",
            "source_authority": authority,
            "source": "portfolio_risk",
            "title": title,
            "lifecycle_state": "unavailable" if identity_missing else "blocked" if severity == "critical" else "current",
            "transition": None,
            "current_at": _queue_datetime(row.get("updated_at") or row.get("available_at")) or reference,
            "expires_at": _queue_datetime(_queue_value(row, {}, "expires_at", "expiry")),
            "primary_blocker": blocker,
            "next_action": _queue_text(row.get("next_step") or row.get("next_action"), "Review the portfolio risk exception."),
            "drill_down": f"/tickers/{quote(ticker)}" if ticker else "/portfolio",
            "ticker": ticker or None,
            "action": "NO_TRADE",
            "rationale": _queue_text(row.get("summary") or row.get("impact")),
        })
    return output


def research_queue(rows: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    reference = _queue_datetime(now) or datetime.now(UTC)
    output: list[dict[str, Any]] = []
    high_priority = {"high", "critical", "urgent", "p0", "p1"}
    for row in rows:
        family = _queue_text(row.get("source_family")).lower()
        source_type = _queue_text(row.get("source_type")).lower()
        priority = _queue_text(row.get("priority") or row.get("severity")).lower()
        blocking = _is_true(row.get("decision_blocking")) or _is_true(row.get("action_required"))
        if family != "research" and source_type != "research" and priority not in high_priority and not blocking:
            continue
        identifier = _queue_text(row.get("id"))
        authority = f"research:{identifier}" if identifier else "research:missing"
        identity_missing = not identifier
        title = _queue_text(row.get("title"), "Research update")
        current_at = _queue_datetime(row.get("date") or row.get("published_at") or row.get("observed_at"))
        if current_at is not None and current_at > reference:
            continue
        expiry_value = _queue_value(row, {}, "expires_at", "expiry")
        expiry = _queue_datetime(expiry_value)
        expiry_invalid = expiry_value is not None and expiry is None
        expired = expiry is not None and expiry <= reference
        ticker = _queue_text(row.get("primary_symbol") or row.get("symbol")).upper() or None
        blocker = _queue_text(row.get("primary_blocker") or row.get("blocker")) or None
        if identity_missing:
            blocker = "research_identity_missing"
        elif blocking and not blocker:
            blocker = "research_decision_blocked"
        output.append({
            "projection_identity": authority,
            "source_authority": authority,
            "source": "research",
            "title": title,
            "lifecycle_state": "unavailable" if identity_missing or expiry_invalid else "expired" if expired else "current",
            "transition": None,
            "current_at": current_at or reference,
            "expires_at": expiry,
            "primary_blocker": blocker,
            "next_action": _queue_text(row.get("next_action") or row.get("next_step"), "Review the source evidence."),
            "drill_down": _queue_text(row.get("source_url"), "/sources"),
            "ticker": ticker,
            "action": "NO_TRADE",
            "rationale": _queue_text(row.get("thesis") or row.get("summary") or row.get("reason")),
        })
    return output


def dedupe_queue(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for row in rows:
        identity = str(row.get("projection_identity") or "")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        output.append(row)
    return output


def _bounded_today_queue(
    capital_actions: list[dict[str, Any]],
    inbox_actions: list[dict[str, Any]],
    portfolio_risk_actions: list[dict[str, Any]],
    research_actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep capital priority while reserving one slot for each other source."""

    secondary = (inbox_actions, portfolio_risk_actions, research_actions)
    reserved = [rows[0] for rows in secondary if rows]
    capital_limit = max(0, ACTION_QUEUE_LIMIT - len(reserved))
    queue = [*capital_actions[:capital_limit], *reserved]
    queue.extend(capital_actions[capital_limit:])
    queue.extend(row for rows in secondary for row in rows[1:])
    return dedupe_queue(queue)[:ACTION_QUEUE_LIMIT]


def _is_unranked_missing_plan_action(row: dict[str, Any]) -> bool:
    return (
        _is_unranked_today_action(row)
        and row.get("primary_blocker") == "trade_plan_missing"
    )


def _is_unranked_today_action(row: dict[str, Any]) -> bool:
    return (
        row.get("source") == "capital_action"
        and row.get("trade_plan") is None
        and row.get("trade_rank") is None
        and not _has_positive_research_rank(row.get("research_rank"))
        and not row.get("owned")
    )


def _has_positive_research_rank(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    text = value if isinstance(value, str) else str(value) if isinstance(value, int) else ""
    return bool(text) and len(text) <= 9 and text.isascii() and text.isdecimal() and text[0] != "0"


def _queue_value(row: dict[str, Any], payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if row.get(name) is not None:
            return row[name]
        if payload.get(name) is not None:
            return payload[name]
    return None


def _queue_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _queue_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _queue_text(value).lower() in {"1", "true", "yes", "y"}


def _rank_reason(rank: dict[str, Any] | None) -> str:
    if rank is None:
        return "opportunity_rank_missing"
    return str(rank.get("trade_rank_unavailable_reason") or "opportunity_rank_unavailable")


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
