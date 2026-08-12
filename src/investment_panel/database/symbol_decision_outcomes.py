"""Point-in-time outcome evaluation for stock decisions only.

This evaluates research decisions.  It does not create, stage, or route any
stock paper order.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


SECTOR_ETFS = {
    "communication services": "XLC",
    "consumer discretionary": "XLY",
    "consumer staples": "XLP",
    "energy": "XLE",
    "financials": "XLF",
    "health care": "XLV",
    "healthcare": "XLV",
    "industrials": "XLI",
    "materials": "XLB",
    "real estate": "XLRE",
    "technology": "XLK",
    "information technology": "XLK",
    "utilities": "XLU",
}


class SymbolDecisionOutcomeRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def refresh(
        self,
        *,
        now: datetime | None = None,
        lookback_days: int = 400,
        limit: int = 500,
    ) -> dict[str, Any]:
        reference = _utc(now)
        with self.runtime.read(JOB_PROFILE) as connection:
            decisions = connection.execute(
                """
                SELECT decision.id::text AS decision_id, decision.instrument_id,
                       decision.as_of, decision.quality_status, decision.quarantine_reason,
                       instrument.symbol, instrument.sector,
                       outcome.state AS prior_state
                FROM analysis.decision decision
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                LEFT JOIN analysis.symbol_decision_outcome outcome ON outcome.decision_id = decision.id
                WHERE lower(decision.kind) IN ('symbol', 'stock', 'equity')
                  AND decision.as_of <= %s
                  AND decision.as_of >= %s - make_interval(days => %s)
                  AND coalesce(outcome.state, 'observing') <> 'resolved'
                ORDER BY decision.as_of, decision.id
                LIMIT %s
                """,
                [reference, reference, max(1, int(lookback_days)), max(1, min(int(limit), 1000))],
            ).fetchall()
            benchmark_ids = {
                str(row["symbol"]): int(row["id"])
                for row in connection.execute(
                    "SELECT id, symbol FROM catalog.instrument WHERE symbol = ANY(%s::text[])",
                    [["SPY", *sorted(set(SECTOR_ETFS.values()))]],
                ).fetchall()
            }
        updated = 0
        resolved = 0
        quarantined = 0
        for row in decisions:
            decision = dict(row)
            outcome = self._evaluate(decision, reference, benchmark_ids)
            with self.runtime.transaction(JOB_PROFILE) as connection:
                connection.execute(
                    """
                    INSERT INTO analysis.symbol_decision_outcome (
                        decision_id, instrument_id, as_of, available_at, state,
                        return_1d, return_5d, return_20d,
                        spy_adjusted_return_1d, spy_adjusted_return_5d, spy_adjusted_return_20d,
                        sector_adjusted_return_1d, sector_adjusted_return_5d, sector_adjusted_return_20d,
                        mae, mfe, max_drawdown, thesis_invalidated_at,
                        sample_eligible, quarantine_reason, measured_through, metadata, updated_at
                    ) VALUES (
                        %s::uuid, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, now()
                    )
                    ON CONFLICT (decision_id) DO UPDATE SET
                        available_at = EXCLUDED.available_at, state = EXCLUDED.state,
                        return_1d = EXCLUDED.return_1d, return_5d = EXCLUDED.return_5d,
                        return_20d = EXCLUDED.return_20d,
                        spy_adjusted_return_1d = EXCLUDED.spy_adjusted_return_1d,
                        spy_adjusted_return_5d = EXCLUDED.spy_adjusted_return_5d,
                        spy_adjusted_return_20d = EXCLUDED.spy_adjusted_return_20d,
                        sector_adjusted_return_1d = EXCLUDED.sector_adjusted_return_1d,
                        sector_adjusted_return_5d = EXCLUDED.sector_adjusted_return_5d,
                        sector_adjusted_return_20d = EXCLUDED.sector_adjusted_return_20d,
                        mae = EXCLUDED.mae, mfe = EXCLUDED.mfe,
                        max_drawdown = EXCLUDED.max_drawdown,
                        thesis_invalidated_at = EXCLUDED.thesis_invalidated_at,
                        sample_eligible = EXCLUDED.sample_eligible,
                        quarantine_reason = EXCLUDED.quarantine_reason,
                        measured_through = EXCLUDED.measured_through,
                        metadata = EXCLUDED.metadata, updated_at = now()
                    """,
                    [
                        decision["decision_id"], decision["instrument_id"], decision["as_of"],
                        outcome["available_at"], outcome["state"],
                        outcome["return_1d"], outcome["return_5d"], outcome["return_20d"],
                        outcome["spy_adjusted_return_1d"], outcome["spy_adjusted_return_5d"], outcome["spy_adjusted_return_20d"],
                        outcome["sector_adjusted_return_1d"], outcome["sector_adjusted_return_5d"], outcome["sector_adjusted_return_20d"],
                        outcome["mae"], outcome["mfe"], outcome["max_drawdown"],
                        outcome["thesis_invalidated_at"], outcome["sample_eligible"],
                        outcome["quarantine_reason"], outcome["measured_through"], Jsonb(outcome["metadata"]),
                    ],
                )
            updated += 1
            resolved += int(outcome["state"] == "resolved")
            quarantined += int(outcome["state"] == "quarantined")
        return {
            "status": "ok", "evaluated": len(decisions), "updated": updated,
            "resolved": resolved, "quarantined": quarantined,
            "paper_execution_supported": False,
        }

    def _evaluate(
        self,
        decision: dict[str, Any],
        reference: datetime,
        benchmark_ids: dict[str, int],
    ) -> dict[str, Any]:
        with self.runtime.read(JOB_PROFILE) as connection:
            entry = connection.execute(
                "SELECT price, available_at, observed_at FROM raw.current_price_at(%s, ARRAY[%s::bigint])",
                [decision["as_of"], decision["instrument_id"]],
            ).fetchone()
            invalidation = connection.execute(
                """
                SELECT created_at FROM app.thesis_review_event
                WHERE instrument_id = %s AND outcome IN ('invalidated', 'closed')
                  AND created_at >= %s AND created_at <= %s
                ORDER BY created_at LIMIT 1
                """,
                [decision["instrument_id"], decision["as_of"], reference],
            ).fetchone()
            if entry is None or _number(entry["price"]) is None:
                return _quarantined("entry_price_unavailable_at_decision_time", decision, reference)
            entry_price = float(entry["price"])
            entry_date = _market_date(decision["as_of"])
            marks = _daily_marks(connection, int(decision["instrument_id"]), entry_date, reference)
            spy_entry = _current_price(connection, benchmark_ids.get("SPY"), decision["as_of"])
            spy_marks = _daily_marks(connection, benchmark_ids["SPY"], entry_date, reference) if benchmark_ids.get("SPY") else []
            sector_symbol = SECTOR_ETFS.get(str(decision.get("sector") or "").strip().lower())
            sector_entry = _current_price(connection, benchmark_ids.get(sector_symbol or ""), decision["as_of"])
            sector_marks = _daily_marks(connection, benchmark_ids[sector_symbol], entry_date, reference) if sector_symbol and benchmark_ids.get(sector_symbol) else []
        values = [float(mark["close"]) for mark in marks]
        returns = _horizon_returns(entry_price, marks)
        spy = _horizon_returns(spy_entry, spy_marks) if spy_entry else {}
        sector = _horizon_returns(sector_entry, sector_marks) if sector_entry else {}
        quality = str(decision.get("quality_status") or "ok").lower()
        quarantine_reason = str(decision.get("quarantine_reason") or "") or None
        # The option-episode migration marked old non-option rows this way.
        # For this stock-only evaluator it is a type marker, not data damage.
        if quarantine_reason == "not_option_decision":
            quarantine_reason = None
        if quality in {"invalid", "lookahead_blocked"}:
            quarantine_reason = quality
        state = "quarantined" if quarantine_reason else ("resolved" if len(marks) >= 20 else "observing")
        measured = marks[-1]["available_at"] if marks else entry["available_at"]
        return {
            "state": state,
            "available_at": measured,
            "return_1d": returns.get(1), "return_5d": returns.get(5), "return_20d": returns.get(20),
            "spy_adjusted_return_1d": _subtract(returns.get(1), spy.get(1)),
            "spy_adjusted_return_5d": _subtract(returns.get(5), spy.get(5)),
            "spy_adjusted_return_20d": _subtract(returns.get(20), spy.get(20)),
            "sector_adjusted_return_1d": _subtract(returns.get(1), sector.get(1)),
            "sector_adjusted_return_5d": _subtract(returns.get(5), sector.get(5)),
            "sector_adjusted_return_20d": _subtract(returns.get(20), sector.get(20)),
            "mae": min([0.0, *((value / entry_price - 1) for value in values)]),
            "mfe": max([0.0, *((value / entry_price - 1) for value in values)]),
            "max_drawdown": _max_drawdown([entry_price, *values]),
            "thesis_invalidated_at": invalidation["created_at"] if invalidation else None,
            "sample_eligible": state != "quarantined",
            "quarantine_reason": quarantine_reason,
            "measured_through": measured,
            "metadata": {
                "entry_price": entry_price,
                "entry_available_at": entry["available_at"].isoformat(),
                "entry_observed_at": entry["observed_at"].isoformat(),
                "mark_count": len(marks),
                "benchmark": {"spy": "SPY" if spy_entry else None, "sector": sector_symbol if sector_entry else None},
                "paper_execution_supported": False,
            },
        }


def _daily_marks(connection: Any, instrument_id: int, after_date: date, reference: datetime) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        WITH fact AS (
          SELECT * FROM raw.price_bar
          UNION ALL
          SELECT * FROM raw.price_bar_history
        ), confirmed AS (
          SELECT DISTINCT ON (bar.trading_date) bar.trading_date, bar.close,
                 bar.available_at, bar.observed_at
          FROM fact bar
          WHERE bar.instrument_id = %s AND bar.interval = '1d'
            AND bar.trading_date > %s AND bar.available_at <= %s
            AND ((bar.trading_date::timestamp + time '16:00') AT TIME ZONE 'America/New_York') <= %s
            AND EXISTS (
              SELECT 1
              FROM raw.price_bar_confirmation confirmation
              JOIN ingest.run run ON run.id = confirmation.ingest_run_id
              WHERE confirmation.fact_id = bar.id
                AND confirmation.fact_available_at = bar.available_at
                AND run.status IN ('succeeded', 'partial')
                AND run.finished_at IS NOT NULL AND run.finished_at <= %s
            )
          ORDER BY bar.trading_date, bar.available_at DESC, bar.observed_at DESC
        )
        SELECT trading_date, close, available_at, observed_at FROM confirmed
        ORDER BY trading_date LIMIT 25
        """,
        [instrument_id, after_date, reference, reference, reference],
    ).fetchall()
    return [dict(row) for row in rows if _number(row["close"]) is not None]


def _current_price(connection: Any, instrument_id: int | None, as_of: datetime) -> float | None:
    if instrument_id is None:
        return None
    row = connection.execute(
        "SELECT price FROM raw.current_price_at(%s, ARRAY[%s::bigint])", [as_of, instrument_id]
    ).fetchone()
    return _number(row["price"]) if row else None


def _horizon_returns(entry: float | None, marks: list[dict[str, Any]]) -> dict[int, float]:
    if entry is None or entry <= 0:
        return {}
    return {
        horizon: float(marks[horizon - 1]["close"]) / entry - 1
        for horizon in (1, 5, 20)
        if len(marks) >= horizon
    }


def _max_drawdown(values: list[float]) -> float | None:
    if not values or any(value <= 0 for value in values):
        return None
    high = values[0]
    drawdown = 0.0
    for value in values:
        high = max(high, value)
        drawdown = min(drawdown, value / high - 1)
    return drawdown


def _subtract(value: float | None, benchmark: float | None) -> float | None:
    return value - benchmark if value is not None and benchmark is not None else None


def _quarantined(reason: str, decision: dict[str, Any], reference: datetime) -> dict[str, Any]:
    return {
        "state": "quarantined", "available_at": reference,
        "return_1d": None, "return_5d": None, "return_20d": None,
        "spy_adjusted_return_1d": None, "spy_adjusted_return_5d": None, "spy_adjusted_return_20d": None,
        "sector_adjusted_return_1d": None, "sector_adjusted_return_5d": None, "sector_adjusted_return_20d": None,
        "mae": None, "mfe": None, "max_drawdown": None, "thesis_invalidated_at": None,
        "sample_eligible": False, "quarantine_reason": reason, "measured_through": None,
        "metadata": {"decision_as_of": decision["as_of"].isoformat(), "paper_execution_supported": False},
    }


def _market_date(value: datetime) -> date:
    return value.astimezone(ZoneInfo("America/New_York")).date()


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    return current.astimezone(UTC) if current.tzinfo is not None else current.replace(tzinfo=UTC)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
