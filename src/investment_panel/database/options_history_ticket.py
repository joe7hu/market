"""Shared-ticket projection for one QQQ option-history decision row."""

from __future__ import annotations

from typing import Any

from investment_panel.core.option_trade_ticket import build_option_trade_ticket, calibrated_cohort_ready
from investment_panel.database.options_risk_context import option_risk_contexts
from investment_panel.database.runtime import DatabaseRuntime


def published_candidates(
    runtime: DatabaseRuntime,
    rows: list[Any],
    *,
    sleeve_capital: float | None,
) -> list[dict[str, Any]]:
    contexts = option_risk_contexts(
        runtime,
        {str(row["symbol"]) for row in rows},
        evaluated_at=rows[0]["as_of"] if rows else None,
    )
    return [
        published_candidate(
            row,
            sleeve_capital=sleeve_capital,
            risk_context=contexts.get(str(row["symbol"]), {}),
        )
        for row in rows
    ]


def published_candidate(
    raw: Any,
    *,
    sleeve_capital: float | None,
    risk_context: dict[str, float | None],
) -> dict[str, Any]:
    row = dict(raw)
    details = dict(row.get("details") or {})
    scenario = dict(details.get("historical_paths") or {})
    calibration = dict(details.get("calibration") or {})
    ticket = build_option_trade_ticket(
        decision_id=str(row["decision_id"]),
        symbol=str(row["symbol"]),
        structure=str(row["structure"]),
        expiration=row["expiration"],
        legs=[dict(leg) for leg in row["synthetic_legs"] or []],
        entry_price=row["entry_price"],
        one_unit_max_loss=row["max_loss"],
        state=str(row["paper_state"]),
        blockers=list(row["blockers"] or []),
        evaluated_at=row["as_of"],
        market_session=str(row["market_session"] or ""),
        sleeve_capital=sleeve_capital,
        **risk_context,
        thesis=dict(details.get("thesis") or {}),
        forecast={
            "expected_value": row["expected_value"],
            "lower_95_expected_value": scenario.get("lower_95_expected_value"),
            "probability_profit": scenario.get("probability_profit"),
            "probability_semantics": (
                "calibrated_exact_cohort"
                if calibrated_cohort_ready(calibration)
                else "provisional_uncalibrated"
            ),
            "effective_sample_size": calibration.get("sample_size"),
        },
        provenance={
            "quote_source": "robinhood_option_history",
            "revisions": {"model": row["model_version"]},
        },
    )
    return {
        "stable_key": str(row["decision_id"]), "decision_id": str(row["decision_id"]),
        "instrument_id": int(row["instrument_id"]), "as_of": row["as_of"],
        "paper_state": row["paper_state"], "discovery_lane": row["discovery_lane"],
        "structure": row["structure"], "entry_price": row["entry_price"], "max_loss": row["max_loss"],
        "expected_value": row["expected_value"], "data_confidence": row["data_confidence"],
        "execution_confidence": row["execution_confidence"], "market_regime": row["market_regime"],
        "model_version": row["model_version"], "relative_value_id": row["relative_value_id"],
        "modeled_net_edge": row["modeled_net_edge"], "quote_observed_at": row["quote_observed_at"],
        "leg_quotes": list(row["synthetic_legs"] or []),
        "expiration": row["expiration"], "strike": row["strike"], "option_type": row["option_type"],
        "ticket": ticket, "execution_ready": ticket["state"] == "READY", "paper_only": True,
    }
