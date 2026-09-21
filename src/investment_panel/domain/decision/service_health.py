"""Functional health of the monitored decision service, independent of HTTP 200.

WAIT / no validated edge / an immature outcome cohort are legitimate results.
An absent input, broken publication contract or expired decision is not.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Mapping, Sequence

from investment_panel.domain.decision.reference_signal import build_reference_signal, project_reference_signal


# Operational faults only. An unvalidated strategy, negative utility, a risk
# limit, no position, no edge or an unexpired forecast are business outcomes,
# not outages. Keep this explicit rather than matching every "missing" string.
ALLOCATION_PRODUCERS = {
    "fresh_postgres_account_facts_required": "update_broker_account",
    "future_account_revision_not_allowed": "update_broker_account",
    "postgresql_account_facts_required": "update_broker_account",
    "stock_nav_missing": "update_broker_account",
    "option_nav_missing": "update_broker_account",
    "crypto_portfolio_value_missing": "update_broker_account",
    "market_state_missing": "refresh_market_publication",
    "market_state_unavailable": "refresh_market_publication",
    "market_state_publication_missing": "refresh_market_publication",
    "opportunity_rank_missing": "refresh_decision_models",
    "opportunity_rank_identity_mismatch": "refresh_decision_models",
    "alpha_signal_identity_mismatch": "refresh_decision_models",
    "strategy_forecast_identity_mismatch": "refresh_decision_models",
    "selected_expression_missing": "refresh_decision_models",
    "risk_policy_snapshot_missing": "refresh_decision_models",
    "risk_policy_snapshot_mismatch": "refresh_decision_models",
    "entry_limit_missing": "refresh_decision_models",
    "quantity_missing": "refresh_decision_models",
    "max_loss_missing": "refresh_decision_models",
    "planned_loss_missing": "refresh_decision_models",
    "invalidation_missing": "refresh_decision_models",
    "profit_exit_missing": "refresh_decision_models",
}


def decision_service_health(rows: Sequence[Mapping[str, Any]], *, now: datetime) -> dict[str, Any]:
    incidents: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row["symbol"])
        crypto = row.get("asset_class") == "crypto"
        quote = dict(row.get("quote") or {})
        source_signal = build_reference_signal(symbol, quote=quote, feature=row.get("feature"),
            now=now, owned=bool(row.get("is_owned")), continuous=crypto)
        decision = row.get("decision") or {}
        faults: list[tuple[str, str, str]] = []
        if source_signal.action == "SERVICE_FAILURE":
            faults.append((source_signal.failure_code or "source_failed", source_signal.summary,
                           source_signal.owner_job or "refresh_assessment_inputs"))
        signal = None
        try:
            signal = project_reference_signal(decision.get("reference_signal"), now=now)
        except (ValueError, TypeError):
            faults.append(("signal_contract_invalid", "Published signal failed schema validation.", "refresh_decision_models"))
        if signal is None:
            faults.append(("signal_not_published", "No versioned trading conditions have been published.", "refresh_decision_models"))
        elif signal.ticker != symbol:
            faults.append(("signal_instrument_mismatch", "Published signal belongs to a different instrument.", "refresh_decision_models"))
        elif signal.action == "SERVICE_FAILURE":
            faults.append((signal.failure_code or "decision_failed", signal.summary, signal.owner_job or "refresh_decision_models"))
        if not decision.get("opportunity_rank") or not decision.get("trade_plan"):
            faults.append(("decision_contract_incomplete", "Decision publication is missing its ranking or trade-plan contract.", "refresh_decision_models"))
        # Measured conditions can work while the capital-decision service is
        # broken. That must not report global green or hide a missing NAV.
        plan = decision.get("trade_plan") or {}
        policy = decision.get("risk_policy_snapshot") or {}
        codes = {*plan.get("blockers", ()), *policy.get("blockers", ())}
        if plan.get("primary_blocker"):
            codes.add(plan["primary_blocker"])
        allocation_faults = {code: ALLOCATION_PRODUCERS[code] for code in codes if code in ALLOCATION_PRODUCERS}
        for code, job in sorted(allocation_faults.items()):
            faults.append((code, f"Capital-decision producer failed: {code.replace('_', ' ')}.", job))
        # Stable incident IDs group duplicates instead of a wall of identical UI warnings.
        deduplicated = {(code, job): reason for code, reason, job in faults}
        for (code, job), reason in deduplicated.items():
            incidents.append({"incident_id": f"{symbol}:{code}", "symbol": symbol, "code": code,
                "capability": "Capital decisions" if code in allocation_faults else "Trading signals", "reason": reason, "job": job,
                "action": f"Restore {job}; the next successful decision publication must clear this check.",
                "href": "/health", "last_observed_at": quote.get("observed_at")})
        items.append({"symbol": symbol, "asset_class": row.get("asset_class"),
            "status": "failed" if faults else "available", "quote_state": source_signal.quote_state,
            "quote_observed_at": source_signal.quote_observed_at, "feature_session": source_signal.feature_session,
            "decision_as_of": decision.get("as_of"), "decision_published_at": decision.get("published_at"),
            "signal_action": signal.action if signal is not None else "SERVICE_FAILURE",
            "conditions_status": "available" if signal is not None and signal.action != "SERVICE_FAILURE" and source_signal.action != "SERVICE_FAILURE" else "failed",
            "capital_status": "failed" if allocation_faults else "available",
            "owner_jobs": sorted({job for _, job in deduplicated})})
    ready = sum(item["status"] == "available" for item in items)
    return {"status": "failed" if incidents and not ready else "partial" if incidents else "available" if items else "not_configured",
        "monitored_count": len(items), "ready_count": ready, "failed_count": len(items)-ready,
        "incidents": incidents, "instruments": items,
        "failures_by_owner": dict(Counter(item["job"] for item in incidents)),
        "basis": "Quote -> completed-session feature -> current signal -> ranking/trade-plan contract; not profitability."}


__all__ = ["decision_service_health"]
