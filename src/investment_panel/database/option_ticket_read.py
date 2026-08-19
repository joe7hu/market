"""Revalidate published option tickets against current execution authority."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from investment_panel.core.decision import is_market_open
from investment_panel.core.option_trade_ticket import TICKET_VERSION, build_option_trade_ticket, ticket_recommendation_fields
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.options_risk_context import option_risk_contexts
from investment_panel.database.runtime import DatabaseRuntime


_DYNAMIC_BLOCKERS = {
    "regular_market_session_required",
    "complete_legs_required",
    "positive_uncrossed_bid_ask_required",
    "displayed_size_required",
    "quote_age_over_120_seconds",
    "long_leg_open_interest_below_100",
    "complete_quote_timestamps_required",
    "interleg_skew_over_5_seconds",
    "single_leg_relative_width_over_20_percent",
    "package_slippage_over_15_percent",
    "options_risk_sleeve_required",
    "one_unit_risk_required",
    "fresh_broker_account_constraints_required",
    "available_risk_budget_below_one_contract",
}


def reconcile_loaded_radar_tables(
    runtime: DatabaseRuntime,
    tables: dict[str, list[dict[str, Any]]],
    config: dict[str, Any],
) -> None:
    if not {"option_radar_opportunity", "option_radar_summary"}.intersection(tables):
        return
    sleeve = ((config.get("analysis") or {}).get("options_decision_system") or {}).get(
        "options_risk_sleeve_capital"
    )
    opportunities = tables.get("option_radar_opportunity")
    if opportunities is None:
        opportunities = AnalysisRepository(runtime).publication_rows(
            "options-radar",
            "option_radar_opportunity",
        )
    current = revalidate_published_tickets(runtime, opportunities, sleeve_capital=sleeve)
    if "option_radar_opportunity" in tables:
        tables["option_radar_opportunity"] = current
    if "option_radar_summary" in tables:
        tables["option_radar_summary"] = reconcile_radar_summary(
            tables["option_radar_summary"],
            current,
        )


def revalidate_published_tickets(
    runtime: DatabaseRuntime,
    rows: list[dict[str, Any]],
    *,
    sleeve_capital: float | None,
) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    contexts = option_risk_contexts(
        runtime,
        {str(row.get("ticker") or row.get("symbol") or "") for row in rows},
        evaluated_at=now,
    )
    return [
        _revalidated_row(
            row,
            now=now,
            sleeve_capital=sleeve_capital,
            risk_context=contexts.get(str(row.get("ticker") or row.get("symbol") or "").upper(), {}),
        )
        for row in rows
    ]


def reconcile_radar_summary(
    summaries: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Overlay live execution state onto an immutable radar publication.

    The publication records the session in which quotes were captured.  That is
    useful provenance, but it is not the current market state.  The Radar header
    uses this summary to decide whether a snapshot is tradeable, so retaining a
    historic ``regular`` value made a days-old Wednesday snapshot look like the
    current regular session on a weekend.  Keep the capture timestamps intact
    and expose the current session separately through the existing fields.
    """
    if not summaries:
        return summaries
    open_now = is_market_open(datetime.now(UTC))
    primary = [row for row in opportunities if row.get("is_primary_structure") is not False]
    ready = sum(row.get("execution_ready") is True for row in primary)
    setup = sum(row.get("state") == "SETUP" for row in primary)
    watch = len(primary) - ready - setup
    return [{
        **summary,
        "market_session": "rth" if open_now else "closed",
        "frozen_to_last_rth": not open_now and bool(summary.get("latest_complete_quote_time")),
        "ready_count": ready,
        "setup_count": setup,
        "watch_count": max(watch, 0),
    } for summary in summaries]


def _revalidated_row(
    source: dict[str, Any],
    *,
    now: datetime,
    sleeve_capital: float | None,
    risk_context: dict[str, float | None],
) -> dict[str, Any]:
    row = dict(source)
    prior = dict(row.get("ticket") or {})
    try:
        compatible_ticket = (
            bool(prior)
            and int(prior.get("ticket_version") or 0) == TICKET_VERSION
        )
    except (TypeError, ValueError):
        compatible_ticket = False
    if not compatible_ticket:
        blocker = (
            "option_trade_ticket_missing"
            if not prior
            else "option_trade_ticket_version_unsupported"
        )
        row["published_state"] = row.get("state")
        row["execution_ready"] = False
        row["blockers"] = sorted({*[str(item) for item in row.get("blockers") or []], blocker})
        if row.get("state") == "READY":
            row["state"] = "WATCH"
        if row.get("paper_state") == "PAPER_READY":
            row["paper_state"] = "WATCH"
        row["ticket"] = None
        row.update(ticket_recommendation_fields(row))
        return row
    risk = dict(prior.get("risk") or {})
    entry = dict(prior.get("entry") or {})
    forecast = dict(prior.get("forecast") or {})
    requested_state = str(row.get("paper_state") or row.get("state") or prior.get("state") or "WATCH")
    persisted_ready = (
        str(prior.get("state") or "").upper() == "READY"
        and source.get("execution_ready") is True
    )
    ticket = build_option_trade_ticket(
        decision_id=str(prior.get("decision_id") or row.get("decision_id") or ""),
        symbol=str(prior.get("symbol") or row.get("ticker") or row.get("symbol") or ""),
        structure=str(prior.get("structure") or row.get("structure") or ""),
        expiration=prior.get("expiration") or row.get("expiration"),
        legs=[dict(leg) for leg in prior.get("legs") or []],
        entry_price=entry.get("limit_price"),
        one_unit_max_loss=risk.get("one_unit_max_loss"),
        secured_cash=risk.get("one_unit_collateral"),
        state=requested_state,
        blockers=[
            blocker for blocker in prior.get("blockers") or []
            if blocker not in _DYNAMIC_BLOCKERS
        ] + ([] if persisted_ready else ["publication_not_execution_ready"]),
        evaluated_at=now,
        market_session="regular" if is_market_open(now) else "closed",
        sleeve_capital=sleeve_capital,
        **risk_context,
        thesis=dict(prior.get("thesis") or {}),
        forecast=forecast,
        provenance={
            **dict(prior.get("provenance") or {}),
            "revalidated_at": now.isoformat(),
            "revisions": dict(prior.get("data_model_revisions") or {}),
        },
    )
    row["published_state"] = row.get("state")
    row["ticket"] = ticket
    row["blockers"] = ticket["blockers"]
    row["execution_ready"] = ticket["state"] == "READY"
    row["advisory_max_contracts"] = ticket["risk"]["recommended_quantity"]
    row["risk_budget"] = ticket["risk"]["available_risk_budget"]
    row["lower_confidence_expectancy_per_max_risk"] = ticket[
        "lower_confidence_expectancy_per_max_risk"
    ]
    if not row["execution_ready"] and row.get("state") == "READY":
        row["state"] = "WATCH"
    if not row["execution_ready"] and row.get("paper_state") == "PAPER_READY":
        row["paper_state"] = "WATCH"
    row.update(ticket_recommendation_fields(row))
    return row
