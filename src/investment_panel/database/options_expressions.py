"""Point-in-time empirical valuation and debit-spread construction."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.analysis.option_expressions import (
    DebitSpreadInputs,
    LongOptionInputs,
    evaluate_call_debit_spread,
    evaluate_long_option,
)
from investment_panel.database.analysis import AnalysisRepository
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
            """,
            [run_id],
        ).fetchall()
        cutoff = max((row["as_of"] for row in rows), default=None)
        histories = _histories(connection, {int(row["instrument_id"]) for row in rows}, cutoff)
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
                    return_stride=max(1, min(int(row["dte"] or 0), 60)),
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
    with runtime.read(JOB_PROFILE) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                WITH ranked_feature AS (
                    SELECT feature.*,
                           row_number() OVER (
                               PARTITION BY contract.underlying_instrument_id, contract.expiration
                               ORDER BY feature.liquidity_score DESC NULLS LAST, contract.strike
                           ) AS expression_rank
                    FROM analysis.option_feature feature
                    JOIN catalog.option_contract contract ON contract.id = feature.contract_id
                    WHERE feature.run_id = %s AND contract.option_type = 'call'
                )
                SELECT feature.snapshot_id, feature.contract_id,
                       feature.quote_observed_at, feature.dte,
                       feature.liquidity_score, contract.underlying_instrument_id AS instrument_id,
                       contract.expiration, contract.strike, contract.multiplier,
                       quote.underlying_price, quote.bid, quote.ask, quote.open_interest
                FROM ranked_feature feature
                JOIN catalog.option_contract contract ON contract.id = feature.contract_id
                JOIN raw.option_quote quote
                  ON quote.snapshot_id = feature.snapshot_id
                 AND quote.contract_id = feature.contract_id
                 AND quote.observed_at = feature.quote_observed_at
                WHERE feature.expression_rank <= 24
                  AND quote.bid >= 0 AND quote.ask > quote.bid
                ORDER BY contract.underlying_instrument_id, contract.expiration, contract.strike
                """,
                [run_id],
            ).fetchall()
        ]
        cutoff = max((row["quote_observed_at"] for row in rows), default=None)
        histories = _histories(connection, {int(row["instrument_id"]) for row in rows}, cutoff)
    grouped: dict[tuple[int, Any], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(int(row["instrument_id"]), row["expiration"])].append(row)
    created = 0
    for (instrument_id, _expiration), chain in grouped.items():
        horizon_returns = _horizon_returns(histories.get(instrument_id, []), int(chain[0]["dte"] or 0))
        if len(horizon_returns) < 20:
            continue
        candidates: list[tuple[float, dict[str, Any], dict[str, Any], Any]] = []
        for index, long_leg in enumerate(chain[:-1]):
            for short_leg in chain[index + 1:index + 4]:
                result = evaluate_call_debit_spread(
                    DebitSpreadInputs(
                        spot=float(long_leg["underlying_price"] or 0),
                        long_strike=float(long_leg["strike"]),
                        short_strike=float(short_leg["strike"]),
                        long_ask=float(long_leg["ask"] or 0),
                        short_bid=float(short_leg["bid"] or 0),
                        multiplier=int(long_leg["multiplier"] or 100),
                        historical_horizon_returns=tuple(horizon_returns),
                        return_stride=max(1, min(int(chain[0]["dte"] or 0), 60)),
                    )
                )
                if result and result.expected_value > 0:
                    candidates.append((result.risk_adjusted_expectancy, long_leg, short_leg, result))
        for _rank, long_leg, short_leg, result in sorted(candidates, reverse=True, key=lambda row: row[0])[:3]:
            repository.store_option_decision(
                run_id,
                decision_key=f"call-debit-spread:{long_leg['contract_id']}:{short_leg['contract_id']}",
                instrument_id=instrument_id,
                contract_id=int(long_leg["contract_id"]),
                snapshot_id=int(long_leg["snapshot_id"]),
                quote_observed_at=long_leg["quote_observed_at"],
                state="READY" if "call_debit_spread" in calibrated_ready else "SETUP",
                score=max(0.0, min(100.0, 50 + 40 * result.risk_adjusted_expectancy)),
                rank=None,
                inputs={"long_leg": long_leg, "short_leg": short_leg, "result": result.as_dict()},
                reasons=("positive_empirical_expectancy", "defined_risk", "expression_compared"),
                details={
                    "quality_status": "complete",
                    "structure": "call_debit_spread",
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
                        "feature_version": "option-professional-v2",
                        "probability_semantics": "provisional_uncalibrated",
                        "same_snapshot_legs": True,
                    },
                },
                strategy_revision_id=strategy_id,
            )
            created += 1
    return created


def _histories(connection: Any, instrument_ids: set[int], cutoff: Any) -> dict[int, list[float]]:
    if not instrument_ids or cutoff is None:
        return {}
    rows = connection.execute(
        """
        SELECT instrument_id, close
        FROM raw.price_bar
        WHERE instrument_id = ANY(%s) AND interval = '1d' AND close > 0
          AND observed_at <= %s
        ORDER BY instrument_id, trading_date, observed_at
        """,
        [list(instrument_ids), cutoff],
    ).fetchall()
    histories: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        histories[int(row["instrument_id"])].append(float(row["close"]))
    return histories


def _horizon_returns(prices: list[float], dte: int) -> list[float]:
    horizon = max(2, min(dte, 60))
    return [prices[index] / prices[index - horizon] - 1 for index in range(horizon, len(prices))]
