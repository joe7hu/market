"""Today read-model composition and action queue policy."""

from __future__ import annotations

from datetime import UTC, date, datetime
from math import isfinite
from typing import Any
from urllib.parse import quote

from investment_panel.application.read_models import panel_snapshot as panel_owner
from investment_panel.infrastructure.postgres.options_research import OptionsResearchRepository
from investment_panel.application.read_models import loaders, payloads
from investment_panel.application.read_models.types import PanelData
from investment_panel.settings import AppConfig
from investment_panel.domain.decision import (
    AvailabilityStatus,
    availability_status_for_blockers,
    build_decision_resolution,
    capital_action_from_resolution,
    resolution_from_published,
    opportunity_rank_blocker,
    next_action_for,
)

ACTION_QUEUE_LIMIT = 10
BRIEF_CATEGORY_LIMITS = {"decide_now": 4, "catalysts": 3, "whats_changed": 3, "portfolio_pulse": 2}


def today(
    config: AppConfig,
    option_actions: OptionsResearchRepository,
    research_repository: Any = None,
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
                resolution = resolution_from_published({**row, "ticker": symbol})
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
        action_identity = _shared_action_identity(
            symbol,
            str(row.get("opportunity_episode_id") or "").strip(),
            revision,
            str(resolution.policy_version or row.get("policy_version") or "").strip(),
        )
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
            "action_identity": action_identity,
            "title": f"{symbol} capital action" if symbol else "Ticker decision needs identity",
            "lifecycle_state": "unavailable" if identity_missing else "blocked" if blocked else "actionable",
            "transition": None,
            "current_at": _queue_datetime(row.get("published_at") or row.get("available_at") or row.get("as_of")),
            "primary_blocker": "ticker_decision_identity_missing" if identity_missing else (rank_reason or "trade_plan_blocked") if blocked else None,
            "next_action": _today_next_action(plan) if plan is not None else "Refresh the ticker decision and trade plan.",
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
            "field_states": today_field_states(
                identity_missing=identity_missing,
                plan_missing=plan is None,
                reason=rank_reason or "trade_plan_missing",
            ),
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
    inbox_rows = _read_inbox(option_actions)
    capital_action_identities = {row.get("action_identity") for row in visible_capital_actions if row.get("action_identity")}
    inbox_actions = [item for item in decision_inbox_queue(inbox_rows) if item.get("action_identity") not in capital_action_identities]
    queue_items = _bounded_today_queue(
        visible_capital_actions,
        inbox_actions,
        _portfolio_risk_queue(panel_data.rows("portfolio_risk_cards")),
        [*research_queue(panel_data.rows("feed_signals")), *_read_learning_actions(research_repository)],
    )
    timestamps = [item["current_at"] for item in queue_items if item.get("current_at") is not None]
    timestamps.extend(
        timestamp
        for name in ("ticker_decisions", "portfolio_risk_cards", "feed_signals")
        if (timestamp := _latest_timestamp(panel_data.rows(name))) is not None
    )
    as_of = max(timestamps, default=None)
    brief_items = _today_brief_items_payload(panel_data.rows("daily_brief"))
    return {
        "status": payloads.status_payload(panel_data),
        "as_of": as_of,
        "actions": queue_items,
        "book_actions": book_action_queue(capital_actions),
        "preopen_brief": _today_preopen_brief_payload(panel_data.rows("preopen_daily_brief")),
        "brief_items": brief_items,
        "brief_categories": _today_brief_categories_payload(panel_data, brief_items),
        "portfolio_risk_items": _today_portfolio_risk_payload(panel_data.rows("portfolio_risk_cards")),
        "missing_plan_count": missing_plan_count,
        "count": len(queue_items),
    }


def _today_resolution_payload(resolution: Any) -> dict[str, Any]:
    return {**resolution.model_dump(mode="json", include={
        "contract_version", "lifecycle", "eligibility", "authorization_mode", "data_quality",
        "action", "trade_plan_id", "primary_blocker", "blockers", "next_action", "policy_version",
        "decision_revision", "ticker", "rationale", "owned", "price_condition", "catalyst", "expires_at",
    }), "next_action": _today_next_action(resolution)}


def _today_trade_plan_payload(plan: Any) -> dict[str, Any]:
    return {**plan.model_dump(mode="json", include={
        "contract_version", "trade_plan_id", "publication_id", "ticker", "opportunity_episode_id",
        "decision_revision", "policy_version", "selected_expression_kind", "selected_expression_identity",
        "rank_id", "alpha_signal_id", "portfolio_impact_id", "market_snapshot_id",
        "market_state_publication_id", "action", "eligibility", "authorization_mode", "data_quality",
        "rationale", "primary_blocker", "blockers", "next_action",
    }), "next_action": _today_next_action(plan)}


def _today_next_action(value: Any) -> str:
    action = value.next_action
    if action not in {"Refresh the required fact and recalculate the resolution.", "Refresh and recalculate the decision."}:
        return action
    blocker = next((reason for reason in value.blockers if reason not in {"cash_comparator", "cash_selected"}), value.primary_blocker)
    return next_action_for(blocker)


def _today_preopen_brief_payload(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    row = rows[0]
    forecast = row.get("qqq_forecast") if isinstance(row.get("qqq_forecast"), dict) else {}
    backtest = row.get("backtest") if isinstance(row.get("backtest"), dict) else {}
    outcome = row.get("qqq_outcome") if isinstance(row.get("qqq_outcome"), dict) else {}
    events = row.get("key_events") if isinstance(row.get("key_events"), list) else []
    return {
        "stable_key": str(row.get("stable_key") or "preopen"),
        "headline": str(row.get("headline") or "Market open brief"),
        "narrative": str(row.get("summary") or ""),
        "macro_regime": _optional_text(row.get("macro_regime")),
        "opening_scenario": _optional_text(row.get("opening_scenario")),
        "qqq_path": _optional_text(row.get("qqq_path")),
        "bias": str(forecast.get("bias") or "neutral"),
        "expected_close": _finite_number(forecast.get("expected_close")),
        "support": _finite_number(forecast.get("support")),
        "resistance": _finite_number(forecast.get("resistance")),
        "expected_return_pct": _finite_number(forecast.get("expected_return_pct")),
        "backtest_mae_pct": _finite_number(backtest.get("mae_pct")),
        "range_hit_rate_pct": _finite_number(backtest.get("range_hit_rate_pct")),
        "outcome_status": str(outcome.get("status") or "pending"),
        "actual_price": _finite_number(outcome.get("actual_price")),
        "actual_return_pct": _finite_number(outcome.get("actual_return_pct")),
        "absolute_error_pct": _finite_number(outcome.get("absolute_error_pct")),
        "within_forecast_range": outcome.get("within_forecast_range") if isinstance(outcome.get("within_forecast_range"), bool) else None,
        "direction_correct": outcome.get("direction_correct") if isinstance(outcome.get("direction_correct"), bool) else None,
        "key_events": [str(item.get("event")) for item in events if isinstance(item, dict) and _optional_text(item.get("event"))],
        "risks": _text_list(row.get("risks")),
        "watch_items": _text_list(row.get("watch_items")),
    }


def _today_brief_items_payload(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for category, limit in BRIEF_CATEGORY_LIMITS.items():
        candidates = [row for row in rows if row.get("category") == category]
        if category == "catalysts":
            candidates.sort(key=lambda row: (_integer(row.get("days_until")) is None, _integer(row.get("days_until")) or 0))
        selected.extend(candidates[:limit])
    return [_today_brief_item_payload(row, index) for index, row in enumerate(selected)]


def _today_brief_categories_payload(panel_data: PanelData, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = panel_data.rows("daily_brief")
    unavailable = not panel_data.status.ready or "daily_brief" in panel_data.metadata.get("unavailable_models", [])
    preopen = next(iter(panel_data.rows("preopen_daily_brief")), {})
    # A successful publication is not proof of full source coverage. Honor
    # explicit producer coverage only when it belongs to this publication.
    same_publication = bool(preopen.get("publication_id")) and all(
        row.get("publication_id") == preopen["publication_id"] for row in rows
    )
    coverage = preopen.get("brief_coverage") if same_publication and isinstance(preopen.get("brief_coverage"), dict) else {}
    output = []
    for category in BRIEF_CATEGORY_LIMITS:
        state = coverage.get(category) if isinstance(coverage.get(category), dict) else {}
        status = "unavailable" if unavailable else str(state.get("status") or "unknown")
        if status not in {"complete", "partial", "unavailable", "unknown"}:
            status = "unknown"
        output.append({
            "category": category,
            "total_count": None if unavailable else sum(row.get("category") == category for row in rows),
            "shown_count": sum(row.get("category") == category for row in items),
            "coverage_status": status,
            "coverage_message": str(state.get("message") or (
                "Daily context could not be loaded. Refresh this view."
                if unavailable else "Source coverage has not been confirmed; an empty list does not mean no events or changes."
                if status == "unknown" else ""
            )),
        })
    return output


def _today_portfolio_risk_payload(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "stable_key": str(row.get("card_id") or f"portfolio-risk:{index}"),
        "category": "portfolio_risk",
        "title": str(row.get("title") or "Portfolio risk"),
        "summary": str(row.get("interpretation") or row.get("summary") or ""),
        "score": _finite_number(row.get("score")) or 0.0,
        "symbol": _optional_text(row.get("symbol")),
        "severity": str(row.get("severity") or "info"),
        "sentiment": str(row.get("sentiment") or "neutral"),
        "action": _optional_text(row.get("action")),
        "next_action": _optional_text(row.get("next_action") or row.get("next_step")),
        "blockers": _text_list(row.get("blockers")),
        "stats": _text_list(row.get("symbols")),
    } for index, row in enumerate(rows)]


def _today_brief_item_payload(row: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "stable_key": str(row.get("stable_key") or f"today-brief:{index}"),
        "category": str(row.get("category") or "uncategorized"),
        "title": str(row.get("headline") or "Decision item"),
        "summary": str(row.get("summary") or ""),
        "score": _finite_number(row.get("score")) or 0.0,
        "symbol": _optional_text(row.get("symbol")),
        "sentiment": str(row.get("sentiment") or "neutral"),
        "severity": str(row.get("severity") or "info"),
        "antithesis": _optional_text(row.get("antithesis")),
        "action": _optional_text(row.get("action")),
        "next_action": _optional_text(row.get("next_action")),
        "blockers": _text_list(row.get("blockers")),
        "days_until": _integer(row.get("days_until")),
        "stats": _today_brief_stats(row),
    }


def _today_brief_stats(row: dict[str, Any]) -> list[str]:
    values = (
        ("Research rank", row.get("research_rank")),
        ("Trade rank", row.get("trade_rank")),
        ("Execution quality", row.get("execution_quality_score")),
        ("Weight", row.get("weight")),
        ("Unrealized P&L", row.get("unrealized_pnl")),
    )
    return [f"{label} {value}" for label, value in values if _finite_number(value) is not None]


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _text_list(value: Any) -> list[str]:
    return [str(item) for item in value if str(item).strip()] if isinstance(value, list) else []


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _integer(value: Any) -> int | None:
    number = _finite_number(value)
    return int(number) if number is not None and number.is_integer() else None


def today_field_states(*, identity_missing: bool, plan_missing: bool, reason: str) -> list[dict[str, Any]]:
    """Describe unavailable decision inputs without treating absence as a value."""

    states: list[dict[str, Any]] = []
    if identity_missing:
        states.append({
            "field": "ticker_decision_id",
            "availability_status": AvailabilityStatus.MISSING,
            "source": "ticker_decision",
            "reason": "ticker_decision_identity_missing",
            "blocking": True,
            "next_action": "Publish a ticker decision with a stable identity.",
        })
    if plan_missing:
        states.append({
            "field": "trade_plan",
            "availability_status": availability_status_for_blockers([reason]),
            "source": "trade_plan",
            "reason": reason,
            "blocking": True,
            "next_action": "Refresh the ticker decision and publish its canonical TradePlan.",
        })
    return states


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
        payload = reader(limit=ACTION_QUEUE_LIMIT, cursor=None, current_only=True)
    except Exception:
        # Inbox is an additive read source. A failed read cannot authorize or
        # revive an action, and the canonical panel status remains available.
        return []
    items = payload.get("items") if isinstance(payload, dict) else []
    return [dict(item) for item in items or [] if isinstance(item, dict)]


def _read_learning_actions(repository: Any) -> list[dict[str, Any]]:
    reader = getattr(repository, "action_items", None)
    if not callable(reader):
        return []
    try:
        rows = reader(limit=ACTION_QUEUE_LIMIT)
    except Exception:
        # Research actions are additive; a read outage must not hide the
        # canonical Today queue or authorize a paper action.
        return []
    return [dict(item) for item in rows or [] if isinstance(item, dict)]


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
        action_identity = _shared_action_identity(
            ticker or "",
            _queue_text(_queue_value(row, payload, "opportunity_episode_id", "episode_id")),
            _queue_text(_queue_value(row, payload, "decision_revision", "revision")),
            _queue_text(_queue_value(row, payload, "policy_version"), "risk-policy.v2:legacy"),
        )
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
            "inbox_item_id": identifier or None,
            "action_identity": action_identity,
            "user_state": _queue_text(row.get("user_state"), "open"),
            "useful": row.get("useful") if isinstance(row.get("useful"), bool) else None,
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


def _shared_action_identity(ticker: str, episode: str, revision: str, policy: str) -> str | None:
    if not ticker or not revision:
        return None
    return ":".join(("decision", ticker.upper(), episode or "episode-missing", revision, policy or "policy-missing"))


def _bounded_today_queue(
    capital_actions: list[dict[str, Any]],
    inbox_actions: list[dict[str, Any]],
    portfolio_risk_actions: list[dict[str, Any]],
    research_actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep capital priority while reserving one slot for each other source."""

    def current(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            row for row in rows
            if row.get("lifecycle_state") not in {"expired", "superseded"}
        ]
    secondary = tuple(current(rows) for rows in (inbox_actions, portfolio_risk_actions, research_actions))
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
    selected = row.get("selected_expression") if isinstance(row.get("selected_expression"), dict) else {}
    reason = opportunity_rank_blocker(rank, selected)
    return reason is None, reason or ""


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


__all__ = ["today", "today_field_states", "book_action_queue", "decision_inbox_queue", "research_queue", "dedupe_queue"]
