"""Incremental, one-row-per-decision option outcome learning."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


class OutcomeRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def refresh(self, *, now: datetime | None = None, lookback_days: int = 365) -> dict[str, Any]:
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("outcome timestamp must be timezone-aware")
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT decision.id::text AS decision_id, decision.as_of, contract.expiration,
                       contract.strike, contract.multiplier, option_decision.structure,
                       option_decision.premium_mid, option_decision.entry_price,
                       option_decision.synthetic_legs,
                       option_decision.secured_cash, option_decision.effective_assignment_price,
                       quote.observed_at, quote.mid, quote.bid, quote.ask, quote.underlying_price,
                       short_quote.mid AS short_mid, short_quote.ask AS short_ask,
                       outcome.maturity_state, outcome.return_1d, outcome.return_5d,
                       outcome.return_20d, outcome.return_60d, outcome.peak_return,
                       outcome.max_drawdown, outcome.time_to_2x_days,
                       outcome.time_to_5x_days, outcome.time_to_10x_days
                FROM analysis.decision decision
                JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
                JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
                JOIN raw.option_snapshot entry_snapshot ON entry_snapshot.id = option_decision.snapshot_id
                LEFT JOIN analysis.option_outcome outcome ON outcome.decision_id = decision.id
                LEFT JOIN raw.option_quote quote
                  ON quote.contract_id = option_decision.contract_id
                 AND quote.observed_at > decision.as_of
                 AND quote.observed_at <= %s
                 AND EXISTS (
                     SELECT 1 FROM raw.option_snapshot mark_snapshot
                     WHERE mark_snapshot.id = quote.snapshot_id
                       AND mark_snapshot.market_session = 'regular'
                       AND mark_snapshot.source_id = entry_snapshot.source_id
                 )
                LEFT JOIN raw.option_quote short_quote
                  ON option_decision.structure = 'call_debit_spread'
                 AND short_quote.contract_id = (option_decision.synthetic_legs->1->>'contract_id')::bigint
                 AND short_quote.observed_at = quote.observed_at
                WHERE decision.kind = 'option' AND decision.state <> 'REJECTED'
                  AND decision.as_of >= %s - make_interval(days => %s)
                  AND (outcome.decision_id IS NULL OR outcome.maturity_state = 'observing'
                       OR option_decision.structure = 'call_debit_spread')
                ORDER BY decision.id, quote.observed_at
                """,
                [reference, reference, lookback_days],
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row["decision_id"])].append(dict(row))
        updated = 0
        matured = 0
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for decision_id, decision_rows in grouped.items():
                first = decision_rows[0]
                entry = _number(first.get("premium_mid"))
                if not entry or entry <= 0:
                    continue
                structure = str(first.get("structure") or "long_option")
                if structure == "cash_secured_put":
                    entry = _number(first.get("entry_price")) or entry
                    secured_cash = _number(first.get("secured_cash"))
                    multiplier = int(first.get("multiplier") or 100)
                    marks = [
                        (
                            row["observed_at"],
                            float(row.get("ask") if _number(row.get("ask")) is not None else row["mid"]),
                            ((entry - float(row.get("ask") if _number(row.get("ask")) is not None else row["mid"])) * multiplier)
                            / secured_cash,
                        )
                        for row in decision_rows
                        if row.get("observed_at") is not None
                        and _number(row.get("mid")) is not None
                        and secured_cash
                    ]
                elif structure == "call_debit_spread":
                    marks = [
                        (
                            row["observed_at"],
                            max(0.0, float(row["bid"]) - float(row["short_ask"])),
                            max(0.0, float(row["bid"]) - float(row["short_ask"])) / entry - 1,
                        )
                        for row in decision_rows
                        if row.get("observed_at") is not None
                        and _number(row.get("bid")) is not None
                        and _number(row.get("short_ask")) is not None
                    ]
                else:
                    marks = [
                        (row["observed_at"], float(row["mid"]), float(row["mid"]) / entry - 1)
                        for row in decision_rows
                        if row.get("observed_at") is not None and _number(row.get("mid")) is not None
                    ]
                as_of = first["as_of"]
                age_days = max(0, (reference.date() - as_of.date()).days)
                expired = first.get("expiration") is not None and first["expiration"] <= reference.date()
                maturity = "expired" if expired else "mature" if age_days >= 60 else "observing"
                underlying_marks = [
                    float(row["underlying_price"])
                    for row in decision_rows
                    if _number(row.get("underlying_price")) is not None
                ]
                strike = _number(first.get("strike"))
                strike_touched = bool(strike and any(price <= strike for price in underlying_marks)) if structure == "cash_secured_put" else None
                latest_underlying = underlying_marks[-1] if underlying_marks else None
                assigned = bool(expired and strike and latest_underlying is not None and latest_underlying < strike)
                paper_status = (
                    "assigned" if assigned else "expired_worthless" if expired and structure == "cash_secured_put" else None
                )
                values = {
                    "return_1d": _horizon_return(marks, as_of, 1, reference),
                    "return_5d": _horizon_return(marks, as_of, 5, reference),
                    "return_20d": _horizon_return(marks, as_of, 20, reference),
                    "return_60d": _horizon_return(marks, as_of, 60, reference),
                    "peak_return": max((mark[2] for mark in marks), default=None),
                    "max_drawdown": min((mark[2] for mark in marks), default=None),
                    "time_to_2x_days": _time_to_multiple(marks, as_of, 1.0),
                    "time_to_5x_days": _time_to_multiple(marks, as_of, 4.0),
                    "time_to_10x_days": _time_to_multiple(marks, as_of, 9.0),
                }
                connection.execute(
                    """
                    INSERT INTO analysis.option_outcome (
                        decision_id, maturity_state, observed_through,
                        current_return, return_1d, return_5d, return_20d, return_60d,
                        peak_return, max_drawdown, time_to_2x_days,
                        time_to_5x_days, time_to_10x_days, paper_status,
                        credit_captured, collateral_return, assigned_basis,
                        strike_touched, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s, %s, now())
                    ON CONFLICT (decision_id) DO UPDATE
                    SET maturity_state = EXCLUDED.maturity_state,
                        observed_through = GREATEST(analysis.option_outcome.observed_through, EXCLUDED.observed_through),
                        current_return = EXCLUDED.current_return,
                        return_1d = COALESCE(analysis.option_outcome.return_1d, EXCLUDED.return_1d),
                        return_5d = COALESCE(analysis.option_outcome.return_5d, EXCLUDED.return_5d),
                        return_20d = COALESCE(analysis.option_outcome.return_20d, EXCLUDED.return_20d),
                        return_60d = COALESCE(analysis.option_outcome.return_60d, EXCLUDED.return_60d),
                        peak_return = EXCLUDED.peak_return,
                        max_drawdown = EXCLUDED.max_drawdown,
                        time_to_2x_days = EXCLUDED.time_to_2x_days,
                        time_to_5x_days = EXCLUDED.time_to_5x_days,
                        time_to_10x_days = EXCLUDED.time_to_10x_days,
                        paper_status = COALESCE(EXCLUDED.paper_status, analysis.option_outcome.paper_status),
                        credit_captured = COALESCE(EXCLUDED.credit_captured, analysis.option_outcome.credit_captured),
                        collateral_return = COALESCE(EXCLUDED.collateral_return, analysis.option_outcome.collateral_return),
                        assigned_basis = COALESCE(EXCLUDED.assigned_basis, analysis.option_outcome.assigned_basis),
                        strike_touched = COALESCE(EXCLUDED.strike_touched, analysis.option_outcome.strike_touched),
                        updated_at = now()
                    """,
                    [
                        decision_id, maturity, max((mark[0] for mark in marks), default=as_of),
                        marks[-1][2] if marks else None,
                        values["return_1d"], values["return_5d"], values["return_20d"], values["return_60d"],
                        values["peak_return"], values["max_drawdown"], values["time_to_2x_days"],
                        values["time_to_5x_days"], values["time_to_10x_days"],
                        paper_status,
                        (entry * int(first.get("multiplier") or 100)) if paper_status == "expired_worthless" else None,
                        marks[-1][2] if marks and maturity != "observing" else None,
                        _number(first.get("effective_assignment_price")) if assigned else None,
                        strike_touched,
                    ],
                )
                updated += 1
                matured += int(maturity != "observing")
        from investment_panel.database.strategy_learning import StrategyLearningRepository

        evaluations = StrategyLearningRepository(self.runtime).refresh_evaluations()
        from investment_panel.database.strategy_governance import StrategyGovernanceRepository

        governance = StrategyGovernanceRepository(self.runtime)
        automatic_promotions = governance.automatic_promote_eligible()
        automatic_rollbacks = governance.rollback_regressing_active()
        return {
            "status": "ok", "outcomes_updated": updated, "outcomes_matured": matured,
            "as_of": reference, **evaluations,
            "automatic_promotions": automatic_promotions,
            "automatic_rollbacks": automatic_rollbacks,
        }


def _horizon_return(
    marks: list[tuple[datetime, float, float]],
    as_of: datetime,
    days: int,
    now: datetime,
) -> float | None:
    target = as_of + timedelta(days=days)
    if now < target:
        return None
    eligible = [mark for mark in marks if target <= mark[0] <= target + timedelta(days=4)]
    return min(eligible, key=lambda mark: mark[0] - target)[2] if eligible else None


def _time_to_multiple(marks: list[tuple[datetime, float, float]], as_of: datetime, threshold: float) -> int | None:
    reached = [mark for mark in marks if mark[2] >= threshold]
    return max(0, (min(reached, key=lambda mark: mark[0])[0].date() - as_of.date()).days) if reached else None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
