"""Shared, deterministic option trade-ticket and risk policy.

The broad Radar collector and the QQQ history collector deliberately remain
independent.  This module is the narrow seam where both must express the same
execution, sizing, exit, and no-trade rules.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from math import floor, isfinite
from typing import Any

from investment_panel.database.opportunity_episodes import option_episode_key
from investment_panel.core.decision import (
    DecisionResolutionV2,
    build_decision_resolution,
)
from investment_panel.core.risk_policy import (
    ASSIGNMENT_POLICY_VERSION,
    PortfolioAssignmentPolicy,
    RiskPolicySnapshot,
    coerce_portfolio_assignment_policy,
    compile_risk_policy_snapshot,
)


TICKET_VERSION = 1
MAX_QUOTE_AGE_SECONDS = 120
MAX_INTERLEG_SKEW_SECONDS = 5
MAX_SINGLE_LEG_RELATIVE_WIDTH = 0.20
MAX_SPREAD_SLIPPAGE_FRACTION = 0.15
MIN_LONG_LEG_OPEN_INTEREST = 100


def calibrated_cohort_ready(calibration: dict[str, Any] | None) -> bool:
    active = dict(calibration or {})
    lower = _number(active.get("lower_95_expectancy"))
    brier = _number(active.get("brier_score"))
    return bool(
        int(active.get("sample_size") or 0) >= 30
        and int(active.get("prediction_sample_size") or 0) >= 30
        and lower is not None
        and lower > 0
        and brier is not None
        and brier <= 0.25
        and int(active.get("other_regime_monitoring_count") or 0) >= 5
    )


def build_option_trade_ticket(
    *,
    decision_id: str,
    symbol: str,
    structure: str,
    expiration: date | str,
    legs: list[dict[str, Any]],
    entry_price: float | None,
    one_unit_max_loss: float | None,
    secured_cash: float | None = None,
    state: str,
    blockers: list[str] | tuple[str, ...] = (),
    evaluated_at: datetime | None = None,
    market_session: str | None = None,
    sleeve_capital: float | None = None,
    open_symbol_risk: float = 0.0,
    open_total_defined_risk: float = 0.0,
    open_symbol_csp_collateral: float = 0.0,
    open_total_csp_collateral: float = 0.0,
    broker_available_capital: float | None = None,
    broker_net_liquidation: float | None = None,
    cash_balance: float | None = None,
    buying_power: float | None = None,
    account_observed_at: datetime | None = None,
    account_source: str | None = None,
    thesis: dict[str, Any] | None = None,
    forecast: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    lane: str | None = None,
    episode_key: str | None = None,
    risk_policy_version: str | None = None,
    policy_version: str | None = None,
    risk_policy_snapshot: RiskPolicySnapshot | None = None,
    assignment_policy: PortfolioAssignmentPolicy | dict[str, Any] | None = None,
    decision_resolution: DecisionResolutionV2 | dict[str, Any] | None = None,
    resolution: DecisionResolutionV2 | dict[str, Any] | None = None,
    decision_revision: str | None = None,
    publication_lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the complete ticket; missing authority always produces quantity 0."""

    now = _as_datetime(evaluated_at) or datetime.now(UTC)
    normalized_legs = [_ticket_leg(leg, now) for leg in legs]
    execution = execution_policy(
        normalized_legs,
        structure=structure,
        entry_price=entry_price,
        market_session=market_session,
        evaluated_at=now,
    )
    if isinstance(risk_policy_snapshot, dict):
        policy_snapshot = RiskPolicySnapshot.model_validate(risk_policy_snapshot)
    else:
        policy_snapshot = risk_policy_snapshot or compile_risk_policy_snapshot(
            account_facts={
                "broker_available_capital": broker_available_capital,
                "broker_net_liquidation": broker_net_liquidation,
                "cash_balance": cash_balance,
                "buying_power": buying_power,
                "account_observed_at": account_observed_at,
            },
            sleeve_capital=sleeve_capital,
            policy_kind="standard",
        )
    risk = sizing_policy(
        structure=structure,
        sleeve_capital=sleeve_capital,
        one_unit_max_loss=one_unit_max_loss,
        secured_cash=secured_cash,
        open_symbol_risk=open_symbol_risk,
        open_total_defined_risk=open_total_defined_risk,
        open_symbol_csp_collateral=open_symbol_csp_collateral,
        open_total_csp_collateral=open_total_csp_collateral,
        broker_available_capital=broker_available_capital,
        broker_net_liquidation=broker_net_liquidation,
        risk_policy_snapshot=policy_snapshot,
    )
    active_thesis = dict(thesis or {})
    active_forecast = dict(forecast or {})
    lower_confidence_expected_value = active_forecast.get("lower_confidence_expected_value")
    if lower_confidence_expected_value is None:
        lower_confidence_expected_value = active_forecast.get("lower_95_expected_value")
    direction_blocker = _thesis_direction_blocker(structure, active_thesis)
    all_blockers = sorted(
        set(
            str(item)
            for item in [
                *blockers,
                *execution["blockers"],
                *risk["blockers"],
                *([] if _invalidation(active_thesis) else ["thesis_invalidation_required"]),
                *([direction_blocker] if direction_blocker else []),
                *(
                    []
                    if (_number(lower_confidence_expected_value) or 0.0) > 0
                    else ["positive_lower_confidence_expectancy_required"]
                ),
                *(
                    []
                    if str(active_forecast.get("probability_semantics") or "").lower()
                    not in {"", "provisional", "provisional_uncalibrated", "uncalibrated"}
                    else ["calibrated_probability_required"]
                ),
            ]
            if item
        )
    )
    assignment = coerce_portfolio_assignment_policy(
        assignment_policy,
        paper_assignment_allowed=False,
        thesis_direction=active_thesis.get("direction"),
        required_cash=secured_cash,
        cash_balance=policy_snapshot.cash_balance,
        buying_power=policy_snapshot.buying_power,
        account_as_of=policy_snapshot.account_observed_at,
        account_source=account_source or "postgresql",
        symbol_limit=policy_snapshot.csp_symbol_limit,
        aggregate_limit=policy_snapshot.csp_total_limit,
        evaluated_at=now,
    )
    requested_policy_version = str(policy_version or risk_policy_version or "")
    if requested_policy_version and requested_policy_version != policy_snapshot.policy_version:
        all_blockers.append("risk_policy_version_mismatch")
    if assignment.risk_policy_version and assignment.risk_policy_version != policy_snapshot.policy_version:
        all_blockers.append("risk_policy_version_mismatch")
    if structure == "cash_secured_put" and not assignment.risk_policy_version:
        all_blockers.append("risk_policy_version_required")
    if assignment.assignment_policy_version != ASSIGNMENT_POLICY_VERSION:
        all_blockers.append("assignment_policy_version_mismatch")
    if structure == "cash_secured_put":
        all_blockers = sorted(set([*all_blockers, *assignment.blockers(as_of=now, required_cash=secured_cash, thesis_direction=active_thesis.get("direction"))]))
    requested_ready = str(state).upper() in {"READY", "PAPER_READY"}
    ticket_state = "READY" if requested_ready and not all_blockers and risk["recommended_quantity"] > 0 else (
        "RESEARCH" if str(state).upper() not in {"REJECT", "REJECTED"} else "AUDIT_ONLY"
    )
    exits = exit_policy(
        structure=structure,
        entry_price=entry_price,
        expiration=expiration,
        legs=normalized_legs,
        thesis_invalidation=_invalidation(active_thesis),
        evaluated_on=now.date(),
    )
    limit_price = _positive_number(entry_price)
    maximum_chase = _round_price(limit_price * 1.05) if limit_price is not None and structure != "cash_secured_put" else None
    minimum_credit = limit_price if structure == "cash_secured_put" else None
    if structure == "cash_secured_put" and len(normalized_legs) == 1:
        bid = _positive_number(normalized_legs[0].get("bid"))
        ask = _positive_number(normalized_legs[0].get("ask"))
        if bid is not None and ask is not None and ask >= bid:
            minimum_credit = max(minimum_credit or 0.0, _round_price((bid + ask) / 2.0) or 0.0)
    quote_times = [
        quote_time
        for leg in normalized_legs
        if (quote_time := _as_datetime(leg.get("quote_time"))) is not None
    ]
    valid_until = (
        min(quote_times) + timedelta(seconds=MAX_QUOTE_AGE_SECONDS)
        if len(quote_times) == len(normalized_legs) and quote_times
        else now
    )
    ticket_lane = str(lane or ("qqq" if symbol.upper() == "QQQ" else "radar")).lower()
    resolved_episode_key = episode_key or option_episode_key(
        lane=ticket_lane,
        symbol=symbol,
        strategy=structure,
        contract_ladder_slot=str(normalized_legs[0].get("contract_id") if normalized_legs else decision_id),
        entry_at=now,
    )
    lineage = dict(publication_lineage or {})
    if not lineage and provenance:
        lineage = {
            key: value
            for key, value in dict(provenance).items()
            if key in {"publication_id", "publication_scope", "analysis_run_id", "analysis_cutoff"}
        }
    resolved_policy_version = str(requested_policy_version or policy_snapshot.policy_version)
    resolved_assignment_policy_version = assignment.assignment_policy_version
    resolution_input = decision_resolution or resolution
    if resolution_input is None:
        resolution_blockers = all_blockers if ticket_state == "READY" else all_blockers or ["paper_entry_not_requested"]
        resolution_value = build_decision_resolution(
            action="BUY" if ticket_state == "READY" else "NO_TRADE",
            decision_revision=str(decision_revision or decision_id),
            policy_version=resolved_policy_version,
            assignment_policy_version=resolved_assignment_policy_version,
            provenance={
                **dict(provenance or {}),
                "as_of": now,
                "available_at": now,
                "revisions": {
                    **dict((provenance or {}).get("revisions") or {}),
                    "ticket": TICKET_VERSION,
                },
            },
            ticker=symbol.upper(),
            blockers=resolution_blockers,
            entry={"limit_price": limit_price, "maximum_chase_price": maximum_chase},
            size=risk["recommended_quantity"],
            invalidation=_invalidation(active_thesis),
            exit=exits,
            ttl=valid_until,
            portfolio_context={
                "status": "complete" if risk.get("broker_available_capital") is not None and risk.get("sleeve_capital") is not None else "missing",
                "broker_available_capital": risk.get("broker_available_capital"),
                "sleeve_capital": risk.get("sleeve_capital"),
            },
            data_quality="COMPLETE" if not all_blockers else "INCOMPLETE",
            authorization_mode="PAPER" if ticket_state == "READY" else "NONE",
            rationale="Paper option ticket is ready." if ticket_state == "READY" else "Option ticket remains research-only.",
            expires_at=valid_until,
            blocked=ticket_state != "READY",
        )
    else:
        resolution_value = DecisionResolutionV2.model_validate(resolution_input)
    resolution_version_mismatch = resolution_value.policy_version != resolved_policy_version
    assignment_version_mismatch = (
        resolution_value.assignment_policy_version != resolved_assignment_policy_version
    )
    if resolution_version_mismatch:
        all_blockers.append("risk_policy_version_mismatch")
    if assignment_version_mismatch:
        all_blockers.append("assignment_policy_version_mismatch")
    if all_blockers and ticket_state == "READY":
        ticket_state = "RESEARCH"
    if all_blockers and (
        resolution_version_mismatch
        or assignment_version_mismatch
        or str(resolution_value.action).upper() not in {"NO_TRADE", "AVOID"}
        or str(resolution_value.eligibility).upper() != "BLOCKED"
    ):
        resolution_value = build_decision_resolution(
            action="NO_TRADE",
            decision_revision=str(decision_revision or resolution_value.decision_revision),
            policy_version=resolved_policy_version,
            assignment_policy_version=resolved_assignment_policy_version,
            provenance=resolution_value.provenance,
            ticker=symbol.upper(),
            blockers=all_blockers,
            data_quality="INCOMPLETE",
            authorization_mode="NONE",
            rationale="Option ticket is blocked by an inconsistent or incomplete policy gate.",
            expires_at=valid_until,
            blocked=True,
        )
    return {
        "ticket_version": TICKET_VERSION,
        "decision_id": str(decision_id),
        "lane": ticket_lane,
        "episode_key": resolved_episode_key,
        "execution_ready_at": now.isoformat() if ticket_state == "READY" else None,
        "expires_at": valid_until.isoformat(),
        "risk_policy_version": resolved_policy_version,
        "policy_version": resolved_policy_version,
        "assignment_policy_version": resolved_assignment_policy_version,
        "decision_revision": str(decision_revision or resolution_value.decision_revision),
        "resolution": resolution_value.model_dump(mode="json"),
        "assignment_policy": assignment.snapshot(),
        "publication_lineage": lineage,
        "symbol": symbol.upper(),
        "state": ticket_state,
        "structure": structure,
        "expiration": expiration.isoformat() if isinstance(expiration, date) else str(expiration),
        "legs": normalized_legs,
        "entry": {
            "limit_price": limit_price,
            "maximum_chase_price": maximum_chase,
            "minimum_credit": minimum_credit,
            "valid_until": valid_until.isoformat(),
            "validity_seconds": MAX_QUOTE_AGE_SECONDS,
            "expected_slippage": execution["expected_slippage"],
        },
        "risk": risk,
        "thesis": {
            "summary": active_thesis.get("summary") or active_thesis.get("core_thesis"),
            "catalyst": active_thesis.get("catalyst"),
            "direction": active_thesis.get("direction"),
            "invalidation": _invalidation(active_thesis) or None,
        },
        "exits": exits,
        "forecast": {
            "interval": active_forecast.get("interval"),
            "expected_value": _number(active_forecast.get("expected_value")),
            "lower_confidence_expected_value": _number(lower_confidence_expected_value),
            "probability_profit": _number(active_forecast.get("probability_profit")),
            "probability_semantics": active_forecast.get("probability_semantics"),
            "effective_sample_size": _int_or_none(
                active_forecast.get("effective_sample_size") or active_forecast.get("sample_size")
            ),
            "tail_loss": _number(active_forecast.get("tail_loss")),
            "no_trade_expected_value": 0.0,
        },
        "lower_confidence_expectancy_per_max_risk": expectancy_per_max_risk(
            lower_confidence_expected_value,
            one_unit_max_loss or secured_cash,
        ),
        "blockers": all_blockers,
        "required_next_action": _next_action(all_blockers),
        "data_model_revisions": dict((provenance or {}).get("revisions") or {}),
        "provenance": dict(provenance or {}),
        "paper_only": True,
    }


def execution_policy(
    legs: list[dict[str, Any]],
    *,
    structure: str,
    entry_price: float | None,
    market_session: str | None,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    blockers: list[str] = []
    if str(market_session or "").lower() != "regular":
        blockers.append("regular_market_session_required")
    if not legs:
        blockers.append("complete_legs_required")
        return {"blockers": blockers, "expected_slippage": None}
    observed_times: list[datetime] = []
    midpoint_package = 0.0
    executable_package = 0.0
    for leg in legs:
        bid, ask = _number(leg.get("bid")), _number(leg.get("ask"))
        if bid is None or ask is None or bid <= 0 or ask < bid:
            blockers.append("positive_uncrossed_bid_ask_required")
            continue
        if (_number(leg.get("bid_size")) or 0) <= 0 or (_number(leg.get("ask_size")) or 0) <= 0:
            blockers.append("displayed_size_required")
        quote_time = _as_datetime(leg.get("quote_time"))
        quote_age = (
            (_utc(evaluated_at) - quote_time).total_seconds()
            if evaluated_at is not None and quote_time is not None
            else _number(leg.get("quote_age_seconds"))
        )
        if quote_age is None or quote_age < 0 or quote_age > MAX_QUOTE_AGE_SECONDS:
            blockers.append("quote_age_over_120_seconds")
        observed = quote_time
        if observed is not None:
            observed_times.append(observed)
        if str(leg.get("side")) in {"long", "buy"} and (_number(leg.get("open_interest")) or 0) < MIN_LONG_LEG_OPEN_INTEREST:
            blockers.append("long_leg_open_interest_below_100")
        midpoint = (bid + ask) / 2.0
        direction = -1.0 if str(leg.get("side")) in {"short", "sell"} else 1.0
        midpoint_package += direction * midpoint
        executable_package += direction * (bid if direction < 0 else ask)
        if len(legs) == 1 and midpoint > 0 and (ask - bid) / midpoint > MAX_SINGLE_LEG_RELATIVE_WIDTH:
            blockers.append("single_leg_relative_width_over_20_percent")
    if len(observed_times) != len(legs):
        blockers.append("complete_quote_timestamps_required")
    elif (max(observed_times) - min(observed_times)).total_seconds() > MAX_INTERLEG_SKEW_SECONDS:
        blockers.append("interleg_skew_over_5_seconds")
    slippage = max(executable_package - midpoint_package, 0.0) if len(legs) > 1 else (
        max(executable_package - midpoint_package, 0.0) if legs else None
    )
    debit = _positive_number(entry_price)
    if len(legs) > 1 and debit is not None and slippage is not None and slippage > debit * MAX_SPREAD_SLIPPAGE_FRACTION:
        blockers.append("package_slippage_over_15_percent")
    return {"blockers": sorted(set(blockers)), "expected_slippage": _round_price(slippage)}


def sizing_policy(
    *,
    structure: str,
    sleeve_capital: float | None,
    one_unit_max_loss: float | None,
    secured_cash: float | None,
    open_symbol_risk: float = 0.0,
    open_total_defined_risk: float = 0.0,
    open_symbol_csp_collateral: float = 0.0,
    open_total_csp_collateral: float = 0.0,
    broker_available_capital: float | None = None,
    broker_net_liquidation: float | None = None,
    risk_policy_snapshot: RiskPolicySnapshot | None = None,
) -> dict[str, Any]:
    policy_snapshot = risk_policy_snapshot or compile_risk_policy_snapshot(
        account_facts={
            "broker_available_capital": broker_available_capital,
            "broker_net_liquidation": broker_net_liquidation,
        },
        sleeve_capital=sleeve_capital,
        policy_kind="standard",
    )
    sleeve = _positive_number(sleeve_capital if sleeve_capital is not None else policy_snapshot.sleeve_capital)
    unit = _positive_number(secured_cash if structure == "cash_secured_put" else one_unit_max_loss)
    broker_capital = _positive_number(
        broker_available_capital if broker_available_capital is not None else policy_snapshot.broker_available_capital
    )
    broker_nav = _positive_number(
        broker_net_liquidation
        if broker_net_liquidation is not None
        else policy_snapshot.broker_net_liquidation or broker_available_capital
    )
    blockers: list[str] = list(policy_snapshot.blockers)
    if sleeve is None:
        blockers.append("options_risk_sleeve_required")
    if unit is None:
        blockers.append("one_unit_risk_required")
    if broker_capital is None:
        blockers.append("fresh_broker_account_constraints_required")
    if sleeve is not None and broker_nav is not None and sleeve > broker_nav:
        blockers.append("options_risk_sleeve_exceeds_broker_nav")
    if blockers:
        quantity = 0
        available = 0.0
    elif structure == "cash_secured_put":
        available = min(
            sleeve * policy_snapshot.csp_symbol_fraction - max(open_symbol_csp_collateral, 0.0),
            sleeve * policy_snapshot.csp_total_fraction - max(open_total_csp_collateral, 0.0),
            broker_capital,
        )
        quantity = max(0, floor(max(available, 0.0) / unit))
    else:
        available = min(
            sleeve * policy_snapshot.defined_trade_fraction,
            sleeve * policy_snapshot.defined_symbol_fraction - max(open_symbol_risk, 0.0),
            sleeve * policy_snapshot.defined_total_fraction - max(open_total_defined_risk, 0.0),
            broker_capital,
        )
        quantity = max(0, floor(max(available, 0.0) / unit))
    if unit is not None and quantity == 0 and not blockers:
        blockers.append("available_risk_budget_below_one_contract")
    total = (unit or 0.0) * quantity
    return {
        "sleeve_capital": sleeve,
        "broker_available_capital": broker_capital,
        "broker_net_liquidation": broker_nav,
        "one_unit_max_loss": _positive_number(one_unit_max_loss),
        "one_unit_collateral": _positive_number(secured_cash),
        "available_risk_budget": round(max(available, 0.0), 2),
        "recommended_quantity": quantity,
        "total_risk": round(total, 2),
        "symbol_exposure_after_entry": round(
            (open_symbol_csp_collateral if structure == "cash_secured_put" else open_symbol_risk) + total, 2
        ),
        "total_options_exposure_after_entry": round(
            (open_total_csp_collateral if structure == "cash_secured_put" else open_total_defined_risk) + total, 2
        ),
        "fully_cash_secured": structure == "cash_secured_put",
        "policy_version": policy_snapshot.policy_version,
        "policy_snapshot": policy_snapshot.snapshot(),
        "policy": {
            "defined_trade_fraction": policy_snapshot.defined_trade_fraction,
            "defined_symbol_fraction": policy_snapshot.defined_symbol_fraction,
            "defined_total_fraction": policy_snapshot.defined_total_fraction,
            "csp_symbol_fraction": policy_snapshot.csp_symbol_fraction,
            "csp_total_fraction": policy_snapshot.csp_total_fraction,
        },
        "blockers": blockers,
    }


def exit_policy(
    *,
    structure: str,
    entry_price: float | None,
    expiration: date | str,
    legs: list[dict[str, Any]],
    thesis_invalidation: str,
    evaluated_on: date,
) -> dict[str, Any]:
    entry = _positive_number(entry_price)
    if structure.endswith("debit_spread") and entry is not None and len(legs) >= 2:
        strikes = sorted(float(leg["strike"]) for leg in legs if _number(leg.get("strike")) is not None)
        width = strikes[-1] - strikes[0] if len(strikes) >= 2 else 0.0
        profit = min(2.0 * entry, 0.8 * width) if width > 0 else 2.0 * entry
    elif structure == "cash_secured_put" and entry is not None:
        profit = 0.5 * entry
    else:
        profit = 2.0 * entry if entry is not None else None
    loss = (2.0 if structure == "cash_secured_put" else 0.5) * entry if entry is not None else None
    expiration_date = date.fromisoformat(str(expiration))
    dte = max((expiration_date - evaluated_on).days, 0)
    time_exit_dte = 7 if dte <= 90 else 30 if dte <= 365 else 60
    return {
        "profit_price": _round_price(profit),
        "loss_price": _round_price(loss),
        "time_exit_dte": 21 if structure == "cash_secured_put" else time_exit_dte,
        "thesis_invalidation": thesis_invalidation or None,
        "liquidity_exit": "Exit or cancel if the package no longer satisfies the execution policy.",
    }


def expectancy_per_max_risk(lower_confidence_expected_value: Any, maximum_risk: Any) -> float | None:
    expectancy, risk = _number(lower_confidence_expected_value), _positive_number(maximum_risk)
    return round(expectancy / risk, 6) if expectancy is not None and risk is not None else None


def ticket_recommendation_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Project compact advisory copy from the same row that owns the ticket.

    The radar used to maintain this in a separate recommendation module.  Keeping
    the projection beside the ticket contract makes the ticket the only source of
    execution, sizing, invalidation, and advisory posture semantics.
    """

    structure = str(row.get("structure") or "")
    state = str(row.get("state") or "WATCH").upper()
    blockers = list(row.get("blockers") or [])
    spot = _number(row.get("underlying_price"))
    break_even = _number(row.get("break_even"))
    entry = _number(row.get("entry_price"))
    buy_under = _number(row.get("buy_under"))
    max_profit = _number(row.get("max_profit"))

    short_put = structure == "cash_secured_put"
    paper_ready = state == "READY" and not blockers
    posture = "PAPER_READY" if paper_ready else "RESEARCH_SETUP" if state == "SETUP" else "NO_TRADE"
    verb = "SELL CASH-SECURED PUT" if short_put else "BUY TO OPEN"
    # A research setup is not an order instruction.  In particular, retaining
    # "BUY TO OPEN" on a blocked/uncalibrated ticket makes stale cards look
    # executable when deterministic gates still deny a paper entry.
    action = f"PAPER — {verb}" if paper_ready else "RESEARCH — STRUCTURE REVIEW" if state == "SETUP" else "NO TRADE"
    suggested_limit = entry if short_put else (buy_under if buy_under is not None and buy_under > 0 else entry)
    break_even_move = abs(break_even - spot) / spot if break_even is not None and spot and spot > 0 else None

    if short_put:
        profit_take = entry * 0.5 if entry is not None else None
        exit_plan = "Buy to close near 50% of the entry credit; do not hold through a broken assignment thesis."
        invalidation = f"Underlying closes below the ${break_even:.2f} break-even or the assignment thesis breaks." if break_even is not None else "Assignment thesis or secured-cash capacity breaks."
        limit_label = "minimum_credit"
    else:
        profit_take = entry * 2 if entry is not None else None
        exit_plan = "Take partial or full profit near 2x premium; exit before expiry if the thesis or liquidity deteriorates."
        invalidation = f"Underlying invalidates the thesis before reaching the ${break_even:.2f} expiry break-even." if break_even is not None else "Thesis, liquidity, or risk budget breaks."
        limit_label = "maximum_entry"

    no_trade_reason = None if paper_ready else (str(blockers[0]) if blockers else "forward_calibration_not_mature")
    return {
        "recommendation_state": posture,
        "advisory_action": action,
        "paper_ready": paper_ready,
        "suggested_limit": suggested_limit,
        limit_label: suggested_limit,
        "break_even_move_pct": break_even_move,
        "profit_take_option_price": profit_take,
        "exit_plan": exit_plan,
        "invalidation": invalidation,
        "no_trade_reason": no_trade_reason,
        "payoff_cap": max_profit,
    }


def _ticket_leg(leg: dict[str, Any], evaluated_at: datetime) -> dict[str, Any]:
    observed = _as_datetime(leg.get("observed_at") or leg.get("captured_at") or leg.get("quote_time"))
    return {
        "contract_id": str(leg.get("contract_id") or ""),
        "option_type": str(leg.get("option_type") or ""),
        "side": "sell" if str(leg.get("side") or "").lower() in {"short", "sell"} else "buy",
        "strike": _number(leg.get("strike")),
        "bid": _number(leg.get("bid")),
        "ask": _number(leg.get("ask")),
        "bid_size": _int_or_none(leg.get("bid_size")),
        "ask_size": _int_or_none(leg.get("ask_size")),
        "quote_time": observed.isoformat() if observed else None,
        "quote_age_seconds": (evaluated_at - observed).total_seconds() if observed else None,
        "open_interest": _int_or_none(leg.get("open_interest")),
        "volume": _int_or_none(leg.get("volume")),
    }


def _next_action(blockers: list[str]) -> str:
    if not blockers:
        return "Revalidate the ticket and stage the exact paper quantity at or below the maximum chase price."
    priority = (
        ("options_risk_sleeve_required", "Configure the options risk sleeve."),
        ("thesis_invalidation_required", "Add a concrete thesis or underlying-price invalidation."),
        ("thesis_direction_required", "Add a directional thesis aligned with the option structure."),
        ("thesis_direction_conflicts_with_structure", "Align the option structure with the current thesis direction."),
        ("calibrated_probability_required", "Collect an exact-cohort calibrated forecast."),
        ("positive_lower_confidence_expectancy_required", "Wait for a positive conservative expectancy versus no trade."),
        ("regular_market_session_required", "Re-quote during the regular option session."),
    )
    for blocker, action in priority:
        if blocker in blockers:
            return action
    return "Refresh and revalidate the complete quote package."


def _invalidation(thesis: dict[str, Any]) -> str:
    direct = str(thesis.get("invalidation") or "").strip()
    if direct:
        return direct
    rules = thesis.get("invalidation_rules")
    if not isinstance(rules, list):
        return ""
    return " · ".join(
        str(rule.get("text") or "").strip()
        for rule in rules
        if isinstance(rule, dict) and str(rule.get("text") or "").strip()
    )


def _thesis_direction_blocker(structure: str, thesis: dict[str, Any]) -> str | None:
    direction = str(thesis.get("direction") or "").strip().lower()
    bullish = {"long", "bullish", "neutral_bullish", "up"}
    bearish = {"short", "bearish", "down"}
    expected = (
        bullish
        if structure in {"long_call", "call_debit_spread", "cash_secured_put"}
        else bearish
        if structure in {"long_put", "put_debit_spread"}
        else set()
    )
    if not expected:
        return None
    if not direction:
        return "thesis_direction_required"
    return None if direction in expected else "thesis_direction_conflicts_with_structure"


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _positive_number(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _round_price(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None
