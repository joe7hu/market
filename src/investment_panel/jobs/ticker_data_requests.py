"""Explicit collectors and source-gap reports for ticker DataRequests."""

from __future__ import annotations

from typing import Any

from investment_panel.jobs import update_content_sources, update_market_events


def update_earnings_and_estimates(config_path: str | None = None) -> dict[str, Any]:
    """Collect public event/content context and report the estimate gap honestly."""

    content = update_content_sources.run_research(config_path)
    events = update_market_events.run(config_path)
    return _partial_result(
        field="earnings_revisions",
        required_source="issuer earnings release, SEC filing, and approved estimate vintage",
        source_results={"research_content": content, "event_calendar": events},
        collected_fields=["scheduled_earnings_events", "public_research_content"],
        missing_fields=["earnings_actuals", "guidance_changes", "analyst_estimate_revisions"],
        reason=(
            "The available free collectors provide event dates and source content. "
            "They do not provide a licensed point-in-time analyst estimate history "
            "or a normalized issuer guidance parser."
        ),
        next_job="market-publish-ticker-decisions",
    )


def update_macro_series(config_path: str | None = None) -> dict[str, Any]:
    """Collect the official release calendar and identify missing vintage series."""

    calendar = update_market_events.run(config_path)
    return _partial_result(
        field="macro_regime",
        required_source="FRED real-time vintage, Treasury, and official release calendar",
        source_results={"official_event_calendar": calendar},
        collected_fields=["official_macro_release_calendar"],
        missing_fields=[
            "fred_real_time_vintages",
            "treasury_curve",
            "real_yields",
            "credit_spreads",
            "dollar",
            "oil",
            "macro_release_surprises",
        ],
        reason=(
            "The free collector currently has official release schedules, but no "
            "vintage-valued FRED/Treasury series publication. No current macro "
            "value is inferred from a schedule."
        ),
        next_job="market-publish-ticker-decisions",
    )


def update_short_interest_and_borrow(_config_path: str | None = None) -> dict[str, Any]:
    """Return a runnable, explicit source-utility report without faking flow data."""

    return _partial_result(
        field="short_interest_and_borrow",
        required_source="official short-interest report and approved licensed borrow source",
        source_results={},
        collected_fields=[],
        missing_fields=["settled_short_interest", "borrow_cost", "borrow_utilization"],
        reason=(
            "No approved free borrow or current settled-short-interest collector is "
            "configured. Daily short volume and 13F changes are not substitutes. "
            "A paid dataset is not purchased automatically."
        ),
        next_job=None,
        source_utility_report={
            "decision_use": "Can change bearish-expression selection and squeeze-risk ranges.",
            "minimum_coverage": "Current observations for the published equity benchmark.",
            "required_license": "Approved short-interest and borrow provider license.",
            "cost_decision": "defer_purchase_until_incremental_predictive_value_is_tested",
        },
    )


def _partial_result(
    *,
    field: str,
    required_source: str,
    source_results: dict[str, Any],
    collected_fields: list[str],
    missing_fields: list[str],
    reason: str,
    next_job: str | None,
    source_utility_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "partial",
        "ok": True,
        "database": "postgresql",
        "source_status": "partial" if source_results else "missing_source",
        "downstream_status": "not_run",
        "data_request": {
            "field": field,
            "required_source": required_source,
            "collected_fields": collected_fields,
            "missing_fields": missing_fields,
            "reason": reason,
        },
        "source_results": source_results,
        "missing_fields": missing_fields,
        "reason": reason,
        "next_job": next_job,
    }
    if source_utility_report is not None:
        result["source_utility_report"] = source_utility_report
    return result
