"""Point-in-time empirical valuation and debit-spread construction."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.analysis.option_expressions import (
    DebitSpreadInputs,
    LongOptionInputs,
    evaluate_call_debit_spread,
    evaluate_long_option,
    evaluate_put_debit_spread,
)
from investment_panel.core.decision import is_us_market_day
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.confirmed_daily_prices import confirmed_daily_bars
from investment_panel.database.options_history_v3_candidates import trading_session_horizon
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


def enrich_long_option_expectancy(
    runtime: DatabaseRuntime, run_id: Any, calibrated_ready: set[str]
) -> int:
    with runtime.read(JOB_PROFILE) as connection:
        rows = connection.execute(
            """
            SELECT decision.id, decision.instrument_id, decision.as_of,
                   feature.dte, feature.liquidity_score, contract.option_type,
                   contract.strike, contract.multiplier, quote.underlying_price,
                   quote.bid, quote.ask
            FROM analysis.decision decision
            JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
            JOIN analysis.option_feature feature
              ON feature.run_id = decision.run_id AND feature.contract_id = option_decision.contract_id
            JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
            JOIN raw.option_quote quote
              ON quote.snapshot_id = option_decision.snapshot_id
             AND quote.contract_id = option_decision.contract_id
             AND quote.observed_at = option_decision.quote_observed_at
            WHERE decision.run_id = %s AND decision.state <> 'REJECTED'
              AND option_decision.structure IN ('long_call', 'long_put')
              AND quote.standard_contract_verified
              AND quote.contract_style = 'american'
              AND quote.contract_settlement = 'physical'
              AND quote.contract_deliverable_key IS NOT NULL
            """,
            [run_id],
        ).fetchall()
        histories = _histories(
            connection, _instrument_cutoffs(rows, field="as_of"), _history_bar_limits(rows)
        )
    updated = 0
    with runtime.transaction(JOB_PROFILE) as connection:
        for source in rows:
            row = dict(source)
            returns = _horizon_returns(histories.get(int(row["instrument_id"]), []), int(row["dte"] or 0))
            result = evaluate_long_option(
                LongOptionInputs(
                    option_type=str(row["option_type"]),
                    spot=float(row["underlying_price"] or 0),
                    strike=float(row["strike"] or 0),
                    ask=float(row["ask"] or 0),
                    bid=float(row["bid"] or 0),
                    multiplier=int(row["multiplier"] or 100),
                    historical_horizon_returns=tuple(returns),
                    return_stride=trading_session_horizon(int(row["dte"] or 0)),
                )
            ) if len(returns) >= 20 else None
            if result is None:
                connection.execute(
                    "UPDATE analysis.decision SET state = 'WATCH', blockers = "
                    "array_append(blockers, 'insufficient_empirical_history') WHERE id = %s",
                    [row["id"]],
                )
                continue
            details = result.as_dict()
            details.update({
                "physical_probability_basis": "point_in_time_empirical_horizon_returns",
                "risk_neutral_probability_basis": "provider_iv_and_delta",
                "probability_semantics": "provisional_uncalibrated",
            })
            score = max(0.0, min(100.0, 50 + 40 * result.risk_adjusted_expectancy + 0.1 * float(row["liquidity_score"] or 0)))
            structure = "long_call" if row["option_type"] == "call" else "long_put"
            state = "READY" if result.expected_value > 0 and structure in calibrated_ready else "SETUP" if result.expected_value > 0 else "WATCH"
            connection.execute(
                """
                UPDATE analysis.decision
                SET state = %s, score = %s,
                    reasons = array_append(reasons, 'empirical_expectancy_evaluated')
                WHERE id = %s
                """,
                [state, round(score, 2), row["id"]],
            )
            connection.execute(
                """
                UPDATE analysis.option_decision
                SET entry_price = %s, exit_cost_estimate = %s,
                    max_profit = %s, max_loss = %s, break_even = %s,
                    probability_profit = %s, expected_value = %s,
                    risk_adjusted_expectancy = %s,
                    required_move_pct = ABS(%s - %s) / NULLIF(%s, 0),
                    details = details::jsonb || %s::jsonb
                WHERE decision_id = %s
                """,
                [
                    result.entry_cost / int(row["multiplier"] or 100),
                    float(row["ask"] or 0) - float(row["bid"] or 0),
                    result.max_profit, result.max_loss, result.break_even,
                    result.probability_profit, result.expected_value,
                    result.risk_adjusted_expectancy, result.break_even,
                    row["underlying_price"], row["underlying_price"],
                    Jsonb(details), row["id"],
                ],
            )
            updated += 1
    return updated


def insert_call_debit_spreads(
    runtime: DatabaseRuntime,
    repository: AnalysisRepository,
    run_id: Any,
    strategy_id: int,
    calibrated_ready: set[str],
) -> int:
    return _insert_debit_spreads(
        runtime, repository, run_id, strategy_id, calibrated_ready, option_type="call"
    )


def insert_put_debit_spreads(
    runtime: DatabaseRuntime,
    repository: AnalysisRepository,
    run_id: Any,
    strategy_id: int,
    calibrated_ready: set[str],
) -> int:
    return _insert_debit_spreads(
        runtime, repository, run_id, strategy_id, calibrated_ready, option_type="put"
    )


def _insert_debit_spreads(
    runtime: DatabaseRuntime,
    repository: AnalysisRepository,
    run_id: Any,
    strategy_id: int,
    calibrated_ready: set[str],
    *,
    option_type: str,
) -> int:
    with runtime.read(JOB_PROFILE) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                WITH ranked_feature AS (
                    SELECT feature.*,
                           row_number() OVER (
                               PARTITION BY contract.underlying_instrument_id, contract.expiration,
                                            contract.multiplier, coalesce(quote.contract_style, ''),
                                            coalesce(quote.contract_settlement, ''),
                                            coalesce(quote.contract_deliverable_key, '')
                               ORDER BY feature.liquidity_score DESC NULLS LAST, contract.strike
                           ) AS expression_rank
                    FROM analysis.option_feature feature
                    JOIN catalog.option_contract contract ON contract.id = feature.contract_id
                    JOIN raw.option_quote quote
                      ON quote.snapshot_id = feature.snapshot_id
                     AND quote.contract_id = feature.contract_id
                     AND quote.observed_at = feature.quote_observed_at
                    WHERE feature.run_id = %s AND contract.option_type = %s
                )
                SELECT feature.snapshot_id, feature.contract_id,
                       feature.quote_observed_at, feature.dte,
                       feature.liquidity_score, contract.underlying_instrument_id AS instrument_id,
                       contract.expiration, contract.strike, contract.multiplier,
                       quote.contract_style AS style,
                       quote.contract_settlement AS settlement,
                       quote.contract_deliverable_key AS deliverable_key,
                       quote.standard_contract_verified,
                       quote.underlying_price, quote.bid, quote.ask, quote.open_interest
                FROM ranked_feature feature
                JOIN catalog.option_contract contract ON contract.id = feature.contract_id
                JOIN raw.option_quote quote
                  ON quote.snapshot_id = feature.snapshot_id
                 AND quote.contract_id = feature.contract_id
                 AND quote.observed_at = feature.quote_observed_at
                WHERE feature.expression_rank <= 24
                  AND quote.contract_style IS NOT NULL AND quote.contract_settlement IS NOT NULL
                  AND quote.contract_deliverable_key IS NOT NULL
                  AND quote.standard_contract_verified
                  AND quote.bid >= 0 AND quote.ask > quote.bid
                ORDER BY contract.underlying_instrument_id, contract.expiration, contract.strike
                """,
                [run_id, option_type],
            ).fetchall()
        ]
        histories = _histories(
            connection, _instrument_cutoffs(rows, field="quote_observed_at"),
            _history_bar_limits(rows),
        )
    grouped: dict[tuple[int, Any, int, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(
            int(row["instrument_id"]), row["expiration"], int(row["multiplier"]),
            str(row["style"]), str(row["settlement"]), str(row["deliverable_key"]),
        )].append(row)
    created = 0
    for (instrument_id, _expiration, _multiplier, _style, _settlement, _deliverable), chain in grouped.items():
        horizon_returns = _horizon_returns(histories.get(instrument_id, []), int(chain[0]["dte"] or 0))
        if len(horizon_returns) < 20:
            continue
        candidates: list[tuple[float, dict[str, Any], dict[str, Any], Any]] = []
        for index, long_leg in enumerate(chain):
            short_legs = (
                chain[index + 1:index + 4]
                if option_type == "call"
                else list(reversed(chain[max(0, index - 3):index]))
            )
            for short_leg in short_legs:
                if not compatible_contract_terms(long_leg, short_leg):
                    continue
                evaluator = evaluate_call_debit_spread if option_type == "call" else evaluate_put_debit_spread
                result = evaluator(
                    DebitSpreadInputs(
                        spot=float(long_leg["underlying_price"] or 0),
                        long_strike=float(long_leg["strike"]),
                        short_strike=float(short_leg["strike"]),
                        long_ask=float(long_leg["ask"] or 0),
                        short_bid=float(short_leg["bid"] or 0),
                        multiplier=int(long_leg["multiplier"] or 100),
                        historical_horizon_returns=tuple(horizon_returns),
                        return_stride=trading_session_horizon(int(chain[0]["dte"] or 0)),
                        option_type=option_type,
                    )
                )
                if result and result.expected_value > 0:
                    candidates.append((result.risk_adjusted_expectancy, long_leg, short_leg, result))
        for _rank, long_leg, short_leg, result in sorted(candidates, reverse=True, key=lambda row: row[0])[:3]:
            structure = "call_debit_spread" if option_type == "call" else "put_debit_spread"
            repository.store_option_decision(
                run_id,
                decision_key=f"{structure}:{long_leg['contract_id']}:{short_leg['contract_id']}",
                instrument_id=instrument_id,
                contract_id=int(long_leg["contract_id"]),
                snapshot_id=int(long_leg["snapshot_id"]),
                quote_observed_at=long_leg["quote_observed_at"],
                state="READY" if structure in calibrated_ready else "SETUP",
                score=max(0.0, min(100.0, 50 + 40 * result.risk_adjusted_expectancy)),
                rank=None,
                inputs={"long_leg": long_leg, "short_leg": short_leg, "result": result.as_dict()},
                reasons=("positive_empirical_expectancy", "defined_risk", "expression_compared"),
                details={
                    "quality_status": "complete",
                    "structure": structure,
                    "premium_mid": result.entry_cost / int(long_leg["multiplier"] or 100),
                    "fill_assumption": result.entry_cost / int(long_leg["multiplier"] or 100),
                    "entry_price": result.entry_cost / int(long_leg["multiplier"] or 100),
                    "max_profit": result.max_profit,
                    "max_loss": result.max_loss,
                    "break_even": result.break_even,
                    "probability_profit": result.probability_profit,
                    "expected_value": result.expected_value,
                    "risk_adjusted_expectancy": result.risk_adjusted_expectancy,
                    "data_confidence": min(1.0, len(horizon_returns) / 100),
                    "execution_confidence": 0.65,
                    "synthetic_legs": [
                        {"side": "buy", "contract_id": str(long_leg["contract_id"]), "strike": float(long_leg["strike"]), "price": float(long_leg["ask"])},
                        {"side": "sell", "contract_id": str(short_leg["contract_id"]), "strike": float(short_leg["strike"]), "price": float(short_leg["bid"])},
                    ],
                    "details": {
                        **result.as_dict(),
                        "feature_version": "option-professional-v3-ticket",
                        "probability_semantics": "provisional_uncalibrated",
                        "same_snapshot_legs": True,
                    },
                },
                strategy_revision_id=strategy_id,
            )
            created += 1
    return created


def compatible_contract_terms(long_leg: dict[str, Any], short_leg: dict[str, Any]) -> bool:
    """Accept vertical legs only when their represented contract terms match."""
    try:
        required = ("style", "settlement", "deliverable_key")
        if long_leg.get("standard_contract_verified") is not True:
            return False
        if short_leg.get("standard_contract_verified") is not True:
            return False
        if any(not str(long_leg.get(key) or "").strip() for key in required):
            return False
        if any(not str(short_leg.get(key) or "").strip() for key in required):
            return False
        return (
            int(long_leg["multiplier"]) == int(short_leg["multiplier"])
            and all(str(long_leg[key]) == str(short_leg[key]) for key in required)
        )
    except (KeyError, TypeError, ValueError):
        return False


def _histories(
    connection: Any, cutoffs: dict[int, Any], bar_limits: dict[int, int],
) -> dict[int, list[float]]:
    if not cutoffs:
        return {}
    return {
        instrument_id: _contiguous_confirmed_closes(
            confirmed_daily_bars(
                connection, [instrument_id], as_of=cutoff,
                max_bars=max(120, bar_limits.get(instrument_id, 120)),
            ).get(instrument_id, [])
        )
        for instrument_id, cutoff in cutoffs.items()
    }


def _history_bar_limits(rows: Any) -> dict[int, int]:
    """Fetch the option horizon plus at least 20 rolling return samples."""
    limits: dict[int, int] = {}
    for row in rows:
        instrument_id = int(row["instrument_id"])
        required = trading_session_horizon(int(row.get("dte") or 0)) + 20
        limits[instrument_id] = max(limits.get(instrument_id, 0), min(required, 700))
    return limits


def _instrument_cutoffs(rows: Any, *, field: str) -> dict[int, Any]:
    cutoffs: dict[int, Any] = {}
    for row in rows:
        instrument_id = int(row["instrument_id"])
        value = row[field]
        if instrument_id not in cutoffs or value > cutoffs[instrument_id]:
            cutoffs[instrument_id] = value
    return cutoffs


def _contiguous_confirmed_closes(rows: list[dict[str, Any]]) -> list[float]:
    ordered = sorted(rows, key=lambda row: row["trading_date"])
    if not ordered or any(not is_us_market_day(row["trading_date"]) for row in ordered):
        return []
    for previous, current in zip(ordered, ordered[1:], strict=False):
        expected = previous["trading_date"] + timedelta(days=1)
        while not is_us_market_day(expected):
            expected += timedelta(days=1)
        if current["trading_date"] != expected:
            return []
    return [float(row["close"]) for row in ordered]


def _horizon_returns(prices: list[float], dte: int) -> list[float]:
    horizon = trading_session_horizon(dte)
    return [prices[index] / prices[index - horizon] - 1 for index in range(horizon, len(prices))]
