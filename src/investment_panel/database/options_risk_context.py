"""Current broker and open-paper exposure inputs for option ticket sizing."""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from typing import Any, Iterable

from investment_panel.database.runtime import DatabaseRuntime


def option_risk_contexts(
    runtime: DatabaseRuntime,
    symbols: Iterable[str],
    *,
    evaluated_at: datetime | None,
) -> dict[str, dict[str, float | None]]:
    normalized = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
    with runtime.read() as connection:
        account = connection.execute(
            """
            SELECT net_liquidation, cash_balance, buying_power, observed_at
            FROM raw.broker_account_snapshot
            ORDER BY observed_at DESC, id DESC LIMIT 1
            """
        ).fetchone()
        rows = connection.execute(
            """
            SELECT instrument.symbol,
              coalesce(sum(CASE WHEN (
                    paper_order.ticket_version IS NOT NULL
                    OR (paper_order.structure IS NOT NULL AND option_decision.decision_id IS NOT NULL)
                  ) AND coalesce(paper_order.structure, '') <> 'cash_secured_put'
                THEN coalesce(
                  (paper_order.ticket_snapshot->'risk'->>'total_risk')::numeric,
                  paper_order.quantity * option_decision.max_loss
                ) ELSE 0 END), 0) AS defined_risk,
              coalesce(sum(CASE WHEN (
                    paper_order.ticket_version IS NOT NULL
                    OR option_decision.decision_id IS NOT NULL
                  ) AND paper_order.structure = 'cash_secured_put'
                THEN paper_order.reserved_collateral ELSE 0 END), 0) AS csp_collateral,
              coalesce(sum(commitment.amount), 0) AS committed_capital,
              count(*) FILTER (WHERE commitment.amount IS NULL) AS unvalued_commitments
            FROM app.paper_order paper_order
            JOIN catalog.instrument instrument ON instrument.id = paper_order.instrument_id
            LEFT JOIN analysis.option_decision option_decision ON option_decision.decision_id = paper_order.decision_id
            CROSS JOIN LATERAL (
              SELECT CASE
                WHEN candidate.amount IS NOT NULL
                  AND candidate.amount > 0
                  AND candidate.amount <> 'NaN'::numeric
                THEN candidate.amount
              END AS amount
              FROM (
                SELECT CASE
                  WHEN paper_order.structure = 'cash_secured_put'
                    THEN paper_order.reserved_collateral
                  ELSE coalesce(
                    (paper_order.ticket_snapshot->'risk'->>'total_risk')::numeric,
                    paper_order.quantity * option_decision.max_loss,
                    paper_order.reserved_collateral,
                    abs(paper_order.quantity * paper_order.limit_price)
                  )
                END AS amount
              ) candidate
            ) commitment
            WHERE paper_order.status IN ('staged', 'open', 'entered')
            GROUP BY instrument.symbol
            """
        ).fetchall()
    by_symbol = {
        str(row["symbol"]): {
            "open_symbol_risk": float(row["defined_risk"] or 0),
            "open_symbol_csp_collateral": float(row["csp_collateral"] or 0),
            "committed_capital": float(row["committed_capital"] or 0),
            "unvalued_commitments": int(row["unvalued_commitments"] or 0),
        }
        for row in rows
    }
    total_defined = sum(float(value["open_symbol_risk"] or 0) for value in by_symbol.values())
    total_csp = sum(float(value["open_symbol_csp_collateral"] or 0) for value in by_symbol.values())
    total_committed = sum(float(value["committed_capital"] or 0) for value in by_symbol.values())
    has_unvalued_commitment = any(
        int(value["unvalued_commitments"] or 0) > 0
        for value in by_symbol.values()
    )
    broker_available = _broker_available(account, evaluated_at)
    broker_nav = (
        float(account["net_liquidation"])
        if broker_available is not None and account is not None
        else None
    )
    if has_unvalued_commitment:
        broker_available = None
        broker_nav = None
    elif broker_available is not None:
        broker_available = max(broker_available - total_committed, 0.0)
    return {
        symbol: {
            **{
                key: value
                for key, value in by_symbol.get(
                    symbol,
                    {
                        "open_symbol_risk": 0.0,
                        "open_symbol_csp_collateral": 0.0,
                        "committed_capital": 0.0,
                        "unvalued_commitments": 0,
                    },
                ).items()
                if key not in {"committed_capital", "unvalued_commitments"}
            },
            "open_total_defined_risk": total_defined,
            "open_total_csp_collateral": total_csp,
            "broker_available_capital": broker_available,
            "broker_net_liquidation": broker_nav,
        }
        for symbol in normalized
    }


def _broker_available(account: Any, evaluated_at: datetime | None) -> float | None:
    if account is None or account["observed_at"] is None or evaluated_at is None:
        return None
    observed = account["observed_at"]
    now = evaluated_at.replace(tzinfo=UTC) if evaluated_at.tzinfo is None else evaluated_at.astimezone(UTC)
    observed = observed.replace(tzinfo=UTC) if observed.tzinfo is None else observed.astimezone(UTC)
    if abs((now - observed).total_seconds()) > 5 * 60:
        return None
    raw_values = (account["net_liquidation"], account["buying_power"], account["cash_balance"])
    if any(value is None for value in raw_values):
        return None
    values = [float(value) for value in raw_values]
    if not all(isfinite(value) and value >= 0 for value in values):
        return None
    return min(values)


broker_available = _broker_available
