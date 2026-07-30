"""Truthful paper-trade and shadow-observation read models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from investment_panel.analysis.history_v3 import MODEL_REVISION
from investment_panel.core.option_underwriting import thesis_invalidation
from investment_panel.database.runtime import DatabaseRuntime


def paper_journal(
    runtime: DatabaseRuntime,
    *,
    symbol: str,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """Return only explicitly staged paper orders, never automatic shadows."""

    return _journal_page(
        runtime,
        symbol=symbol,
        offset=offset,
        limit=limit,
        record_kind="paper_trade",
        source_sql="""
            FROM app.paper_order paper_order
            JOIN analysis.decision decision ON decision.id = paper_order.decision_id
            JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
            JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
            JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
            LEFT JOIN app.thesis thesis ON thesis.id = option_decision.thesis_id
            LEFT JOIN analysis.shadow_trade shadow ON shadow.decision_id = decision.id
            LEFT JOIN analysis.option_outcome outcome ON outcome.decision_id = decision.id
            WHERE instrument.symbol = %s AND option_decision.model_version = %s
        """,
        id_sql="paper_order.id::text AS paper_order_id, shadow.id::text AS shadow_id",
        status_sql="paper_order.status AS paper_status, shadow.status AS shadow_status",
    )


def shadow_observations(
    runtime: DatabaseRuntime,
    *,
    symbol: str,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """Return automatic research observations not promoted into paper orders."""

    return _journal_page(
        runtime,
        symbol=symbol,
        offset=offset,
        limit=limit,
        record_kind="shadow_observation",
        source_sql="""
            FROM analysis.shadow_trade shadow
            JOIN analysis.decision decision ON decision.id = shadow.decision_id
            JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
            JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
            JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
            LEFT JOIN app.thesis thesis ON thesis.id = option_decision.thesis_id
            LEFT JOIN analysis.option_outcome outcome ON outcome.decision_id = decision.id
            LEFT JOIN app.paper_order paper_order ON paper_order.decision_id = decision.id
            WHERE instrument.symbol = %s AND shadow.source_kind = 'options_history_v3'
              AND option_decision.model_version = %s AND paper_order.id IS NULL
        """,
        id_sql="NULL::text AS paper_order_id, shadow.id::text AS shadow_id",
        status_sql="NULL::text AS paper_status, shadow.status AS shadow_status",
    )


def learning_progress(runtime: DatabaseRuntime, *, symbol: str) -> dict[str, Any]:
    """Calibrate only explicitly staged paper trades in exact model cohorts."""

    with runtime.read() as connection:
        rows = connection.execute(
            """
            SELECT option_decision.structure, option_decision.market_regime,
                   option_decision.model_version,
                   count(*) FILTER (
                       WHERE outcome.maturity_state IN ('mature', 'expired')
                         AND outcome.current_return IS NOT NULL
                   ) AS mature_outcomes,
                   avg(outcome.current_return) FILTER (
                       WHERE outcome.maturity_state IN ('mature', 'expired')
                         AND outcome.current_return IS NOT NULL
                   ) AS mean_return,
                   stddev_pop(outcome.current_return) FILTER (
                       WHERE outcome.maturity_state IN ('mature', 'expired')
                         AND outcome.current_return IS NOT NULL
                   ) AS return_stddev,
                   avg(power(option_decision.probability_profit -
                       CASE WHEN outcome.current_return > 0 THEN 1.0 ELSE 0.0 END, 2)
                   ) FILTER (
                       WHERE outcome.maturity_state IN ('mature', 'expired')
                         AND outcome.current_return IS NOT NULL
                         AND option_decision.probability_profit IS NOT NULL
                   ) AS brier_score
            FROM app.paper_order paper_order
            JOIN analysis.decision decision ON decision.id = paper_order.decision_id
            JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
            JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
            LEFT JOIN analysis.option_outcome outcome ON outcome.decision_id = decision.id
            WHERE instrument.symbol = %s AND option_decision.model_version = %s
            GROUP BY option_decision.structure, option_decision.market_regime,
                     option_decision.model_version
            ORDER BY option_decision.structure, option_decision.market_regime,
                     option_decision.model_version
            """,
            [symbol.upper(), MODEL_REVISION],
        ).fetchall()
    progress = [_learning_payload(dict(row)) for row in rows]
    return {"rows": progress, "count": len(progress)}


def _journal_page(
    runtime: DatabaseRuntime,
    *,
    symbol: str,
    offset: int,
    limit: int,
    record_kind: str,
    source_sql: str,
    id_sql: str,
    status_sql: str,
) -> dict[str, Any]:
    with runtime.read() as connection:
        count = connection.execute(f"SELECT count(*) AS count {source_sql}", [symbol.upper(), MODEL_REVISION]).fetchone()["count"]
        rows = connection.execute(
            f"""
            SELECT {id_sql}, decision.id::text AS decision_id, {status_sql},
                   paper_order.limit_price AS order_limit_price, paper_order.created_at AS staged_at,
                   decision.as_of AS decision_at, decision.state AS decision_state,
                   decision.reasons, decision.blockers,
                   option_decision.paper_state, option_decision.discovery_lane,
                   option_decision.model_version, option_decision.max_loss,
                   option_decision.probability_profit, option_decision.expected_value,
                   option_decision.risk_adjusted_expectancy, option_decision.data_confidence,
                   option_decision.execution_confidence, option_decision.modeled_net_edge,
                   option_decision.fair_low, option_decision.fair_high,
                   option_decision.synthetic_legs, option_decision.quote_observed_at,
                   option_decision.details AS decision_details,
                   contract.expiration, contract.strike, contract.option_type, contract.multiplier,
                   thesis.revision AS thesis_revision, thesis.thesis AS thesis_payload,
                   shadow.entry_at, shadow.entry_price, shadow.exit_at, shadow.exit_price,
                   shadow.pending_entry_reason, shadow.entry_cohort_id, option_decision.structure,
                   option_decision.market_regime, shadow.fill_basis, shadow.metrics,
                   outcome.maturity_state AS outcome_state, outcome.observed_through,
                   outcome.current_return, outcome.return_1d, outcome.return_5d,
                   outcome.return_20d, outcome.return_60d, outcome.peak_return,
                   outcome.max_drawdown, outcome.realized_exit_return,
                   outcome.realized_exit_basis, outcome.stock_move_effect,
                   outcome.iv_effect, outcome.theta_effect, outcome.spread_effect,
                   outcome.unexplained_effect
            {source_sql}
            ORDER BY coalesce(shadow.entry_at, paper_order.created_at, shadow.created_at) DESC
            LIMIT %s OFFSET %s
            """,
            [symbol.upper(), MODEL_REVISION, limit, offset],
        ).fetchall()
    return {
        "rows": [_journal_payload(dict(row), record_kind=record_kind) for row in rows],
        "count": int(count),
        "offset": offset,
        "limit": limit,
    }


def _journal_payload(row: dict[str, Any], *, record_kind: str) -> dict[str, Any]:
    metrics = dict(row.get("metrics") or {})
    details = dict(row.get("decision_details") or {})
    thesis = dict(row.get("thesis_payload") or {})
    status = str(row.get("shadow_status") or row.get("paper_status") or "pending")
    outcome_state = row.get("outcome_state")
    lifecycle = str(outcome_state) if outcome_state in {"mature", "expired", "observing"} else status
    latest_mark = _number(metrics.get("mark_price"))
    entry_price = _number(row.get("entry_price")) or _number(row.get("order_limit_price"))
    return {
        "record_kind": record_kind,
        "paper_order_id": row.get("paper_order_id"),
        "shadow_id": row.get("shadow_id"),
        "decision_id": str(row["decision_id"]),
        "lifecycle": lifecycle,
        "structure": row.get("structure"),
        "entry_at": row.get("entry_at"),
        "conservative_entry_price": entry_price,
        "conservative_fill_basis": row.get("fill_basis") or metrics.get("fill_basis"),
        "latest_mark": latest_mark,
        "missing_mark_gap": status == "entered" and latest_mark is None,
        "current_return": _number(row.get("current_return")),
        "outcome_state": outcome_state,
        "pending_entry_reason": row.get("pending_entry_reason"),
        "assignment_warning": metrics.get("assignment_warning")
        or (
            "American-style assignment risk remains shadow-observed."
            if record_kind == "shadow_observation"
            else "American-style assignment risk remains paper-observed."
        ),
        "admission": {
            "decision_at": row.get("decision_at"),
            "decision_state": row.get("decision_state"),
            "paper_state": row.get("paper_state"),
            "discovery_lane": row.get("discovery_lane"),
            "reasons": list(row.get("reasons") or []),
            "blockers": list(row.get("blockers") or []),
            "model_revision": row.get("model_version"),
            "market_regime": row.get("market_regime"),
        },
        "contract": {
            "expiration": _iso(row.get("expiration")),
            "strike": _number(row.get("strike")),
            "option_type": row.get("option_type"),
            "multiplier": int(row.get("multiplier") or 100),
            "legs": list(row.get("synthetic_legs") or []),
        },
        "thesis": {
            "revision": int(row["thesis_revision"]) if row.get("thesis_revision") is not None else None,
            "direction": thesis.get("direction"),
            "core_thesis": thesis.get("core_thesis") or thesis.get("thesis"),
            "invalidation": thesis_invalidation(thesis) or None,
            "horizon_date": thesis.get("horizon_date"),
        },
        "forecast": {
            "probability_profit": _number(row.get("probability_profit")),
            "expected_value": _number(row.get("expected_value")),
            "lower_95_expected_value": _number(
                (details.get("historical_paths") or {}).get("lower_95_expected_value")
            ),
            "max_loss": _number(row.get("max_loss")),
            "risk_adjusted_expectancy": _number(row.get("risk_adjusted_expectancy")),
            "modeled_net_edge": _number(row.get("modeled_net_edge")),
            "fair_value_low": _number(row.get("fair_low")),
            "fair_value_high": _number(row.get("fair_high")),
            "scenario_count": int((details.get("historical_paths") or {}).get("scenario_count") or 0),
            "data_confidence": _number(row.get("data_confidence")),
            "execution_confidence": _number(row.get("execution_confidence")),
        },
        "execution": {
            "staged_at": row.get("staged_at"),
            "signal_quote_at": row.get("quote_observed_at"),
            "entry_cohort_id": row.get("entry_cohort_id"),
            "entry_at": row.get("entry_at"),
            "entry_price": entry_price,
            "fill_basis": row.get("fill_basis") or metrics.get("fill_basis"),
            "latest_mark": latest_mark,
            "exit_at": row.get("exit_at"),
            "exit_price": _number(row.get("exit_price")),
            "holding_period_hours": _holding_hours(row.get("entry_at"), row.get("observed_through")),
        },
        "outcome": {
            "state": outcome_state,
            "observed_through": row.get("observed_through"),
            "current_return": _number(row.get("current_return")),
            "return_1d": _number(row.get("return_1d")),
            "return_5d": _number(row.get("return_5d")),
            "return_20d": _number(row.get("return_20d")),
            "return_60d": _number(row.get("return_60d")),
            "peak_return": _number(row.get("peak_return")),
            "max_drawdown": _number(row.get("max_drawdown")),
            "realized_exit_return": _number(row.get("realized_exit_return")),
            "realized_exit_basis": row.get("realized_exit_basis"),
            "attribution": {
                "underlying": _number(row.get("stock_move_effect")),
                "iv": _number(row.get("iv_effect")),
                "theta": _number(row.get("theta_effect")),
                "spread": _number(row.get("spread_effect")),
                "unexplained": _number(row.get("unexplained_effect")),
            },
        },
        "metrics": metrics,
    }


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _holding_hours(start: Any, end: Any) -> float | None:
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return max(0.0, (end - start).total_seconds() / 3600)


def _learning_payload(row: dict[str, Any]) -> dict[str, Any]:
    outcomes = int(row.get("mature_outcomes") or 0)
    mean_return = _number(row.get("mean_return"))
    stddev = _number(row.get("return_stddev"))
    lower = (
        mean_return - 1.96 * stddev / outcomes**0.5
        if mean_return is not None and stddev is not None and outcomes > 1
        else None
    )
    brier = _number(row.get("brier_score"))
    missing = []
    if outcomes < 30:
        missing.append("30_mature_exact_structure_outcomes_required")
    if lower is None or lower <= 0:
        missing.append("positive_lower_95_expectancy_required")
    if brier is None or brier > 0.25:
        missing.append("brier_score_at_or_below_0_25_required")
    return {
        "structure": row["structure"],
        "market_regime": row.get("market_regime"),
        "model_revision": row["model_version"],
        "mature_outcomes": outcomes,
        "required_mature_outcomes": 30,
        "lower_95_expectancy": lower,
        "brier_score": brier,
        "missing_prerequisites": missing,
    }
