"""Persistence and learning reads for versioned ticker decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.core.decision import Horizon, TickerDecision
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


HORIZON_SESSIONS = {
    Horizon.TACTICAL: (1, 5, 20),
    Horizon.FUNDAMENTAL: (63, 126, 252),
}


class TickerDecisionRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def publish(self, decision: TickerDecision) -> dict[str, Any]:
        """Publish an immutable decision revision and its input manifest."""

        payload = decision.model_dump(mode="json")
        with self.runtime.transaction(JOB_PROFILE) as connection:
            instrument = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = %s LIMIT 1",
                [decision.ticker],
            ).fetchone()
            if instrument is None:
                raise ValueError("ticker instrument is not in the catalog")
            row = connection.execute(
                """
                INSERT INTO analysis.ticker_decision (
                    instrument_id, decision_revision, contract_version, as_of,
                    published_at, input_hash, code_version, experiment_id,
                    tactical, fundamental, capital_action, risk_policy,
                    expressions, selected_expression, data_requests,
                    learning_history, input_manifest, status
                ) VALUES (
                    %s, %s, %s, %s, now(), %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, 'published'
                )
                ON CONFLICT (instrument_id, decision_revision) DO NOTHING
                RETURNING id::text
                """,
                [
                    instrument["id"], decision.decision_revision,
                    decision.decision_contract_version, decision.as_of,
                    decision.input_manifest.input_hash,
                    decision.input_manifest.code_version,
                    decision.input_manifest.experiment_id,
                    Jsonb(payload["tactical"]), Jsonb(payload["fundamental"]),
                    Jsonb(payload["capital_action"]), Jsonb(payload["risk_policy"]),
                    Jsonb(payload["expressions"]), Jsonb(payload.get("selected_expression")),
                    Jsonb(payload["data_requests"]), Jsonb(payload["learning_history"]),
                    Jsonb(payload["input_manifest"]),
                ],
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT id::text FROM analysis.ticker_decision WHERE instrument_id = %s AND decision_revision = %s",
                    [instrument["id"], decision.decision_revision],
                ).fetchone()
            decision_id = str(row["id"])
            connection.execute(
                """
                UPDATE analysis.ticker_decision
                SET status = 'superseded'
                WHERE instrument_id = %s AND id <> %s::uuid AND status = 'published' AND as_of < %s
                """,
                [instrument["id"], decision_id, decision.as_of],
            )
            self._store_manifest(connection, decision_id, decision)
            for request in decision.data_requests:
                connection.execute(
                    """
                    INSERT INTO analysis.ticker_data_request (ticker_decision_id, field, ticker, request)
                    VALUES (%s::uuid, %s, %s, %s)
                    ON CONFLICT (ticker_decision_id, field) DO UPDATE SET request = EXCLUDED.request
                    """,
                    [decision_id, request.field, request.ticker, Jsonb(request.model_dump(mode="json"))],
                )
        return {"status": "published", "ticker_decision_id": decision_id, "decision_revision": decision.decision_revision}

    def latest(self, ticker: str) -> TickerDecision | None:
        with self.runtime.read() as connection:
            row = connection.execute(
                """
                SELECT instrument.symbol AS ticker, decision.contract_version,
                       decision.as_of, decision.decision_revision,
                       decision.tactical, decision.fundamental, decision.capital_action,
                       decision.risk_policy, decision.expressions,
                       decision.selected_expression, decision.data_requests,
                       decision.learning_history, decision.input_manifest
                FROM analysis.ticker_decision decision
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                WHERE instrument.symbol = %s AND decision.status = 'published'
                ORDER BY decision.as_of DESC, decision.created_at DESC
                LIMIT 1
                """,
                [ticker.strip().upper()],
            ).fetchone()
        return _decision_from_row(row) if row else None

    def refresh_outcomes(self, *, now: datetime | None = None, limit: int = 2_000) -> dict[str, int]:
        reference = _utc(now or datetime.now(UTC))
        with self.runtime.read(JOB_PROFILE) as connection:
            decisions = connection.execute(
                """
                SELECT decision.id::text AS decision_id, instrument.id AS instrument_id,
                       instrument.symbol AS ticker, decision.as_of,
                       decision.tactical, decision.fundamental,
                       decision.capital_action, decision.expressions,
                       decision.selected_expression
                FROM analysis.ticker_decision decision
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                WHERE decision.status IN ('published', 'superseded')
                ORDER BY decision.as_of, decision.id
                LIMIT %s
                """,
                [max(1, min(int(limit), 10_000))],
            ).fetchall()
        updated = 0
        resolved = 0
        for decision in decisions:
            row = dict(decision)
            for horizon, sessions in HORIZON_SESSIONS.items():
                for horizon_sessions in sessions:
                    outcome = self._evaluate(row, horizon, horizon_sessions, reference)
                    selected = dict(row.get("selected_expression") or {})
                    self._store_outcome(
                        row["decision_id"], horizon, horizon_sessions, outcome,
                        selected_expression=str(selected.get("kind") or "STOCK"),
                    )
                    updated += 1
                    resolved += int(outcome["state"] == "resolved")
        return {"evaluated": len(decisions), "updated": updated, "resolved": resolved}

    def learning_surface(self, ticker: str) -> dict[str, Any]:
        with self.runtime.read() as connection:
            decision = connection.execute(
                """
                SELECT decision.id::text AS ticker_decision_id, decision.decision_revision,
                       decision.tactical, decision.fundamental, decision.capital_action,
                       decision.expressions
                FROM analysis.ticker_decision decision
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                WHERE instrument.symbol = %s AND decision.status = 'published'
                ORDER BY decision.as_of DESC LIMIT 1
                """,
                [ticker.strip().upper()],
            ).fetchone()
            outcomes = connection.execute(
                """
                SELECT outcome.horizon, outcome.horizon_sessions, outcome.state,
                       outcome.selected_return, outcome.stock_counterfactual_return,
                       outcome.alternate_counterfactual_return, outcome.cash_return,
                       outcome.error_type, outcome.mistake_card
                FROM analysis.ticker_outcome outcome
                JOIN analysis.ticker_decision decision ON decision.id = outcome.ticker_decision_id
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                WHERE instrument.symbol = %s
                ORDER BY outcome.horizon, outcome.horizon_sessions
                """,
                [ticker.strip().upper()],
            ).fetchall()
            episodes = connection.execute(
                """
                SELECT count(DISTINCT decision.id) AS episodes
                FROM analysis.ticker_decision decision
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                JOIN analysis.ticker_outcome outcome ON outcome.ticker_decision_id = decision.id
                WHERE instrument.symbol = %s
                """,
                [ticker.strip().upper()],
            ).fetchone()["episodes"]
        if not decision:
            return {"independent_episode_count": 0, "disagreement": {}, "expression_tournament": [], "mistake_cards": []}
        tactical = dict(decision["tactical"] or {})
        fundamental = dict(decision["fundamental"] or {})
        expressions = dict(decision["expressions"] or {})
        return {
            "independent_episode_count": int(episodes or 0),
            "disagreement": {
                "strongest_bull_case": _first_statement(fundamental.get("evidence_for")),
                "strongest_bear_case": _first_statement(fundamental.get("evidence_against")),
                "resolving_fact": (fundamental.get("fact_that_would_flip") or {}).get("statement"),
            },
            "expression_tournament": [
                {
                    "expression_kind": kind,
                    "selected": bool(value.get("selected")),
                    "status": value.get("status"),
                    "planned_loss": value.get("planned_loss"),
                    "lower_confidence_expectancy": value.get("lower_confidence_expectancy"),
                    "outcomes": [dict(row) for row in outcomes],
                }
                for kind, value in expressions.items()
                if isinstance(value, dict)
            ],
            "mistake_cards": [
                {
                    "horizon": row["horizon"],
                    "horizon_sessions": row["horizon_sessions"],
                    "error_type": row["error_type"],
                    "card": row["mistake_card"] or {},
                }
                for row in outcomes if row["error_type"] or row["mistake_card"]
            ],
        }

    def _store_manifest(self, connection: Any, decision_id: str, decision: TickerDecision) -> None:
        inputs = decision.input_manifest.inputs or {
            "decision_composer": [{
                "source": "deterministic-composer",
                "source_version": decision.input_manifest.code_version,
                "available_at": decision.as_of,
                "input_hash": decision.input_manifest.input_hash,
            }],
        }
        for field, values in inputs.items():
            for value in values if isinstance(values, list) else [values]:
                row = dict(value) if isinstance(value, dict) else {}
                available = _parse_datetime(row.get("available_at") or row.get("as_of") or row.get("observed_at")) or decision.as_of
                if available > decision.as_of:
                    continue
                source = str(row.get("source") or row.get("source_id") or "unknown")
                connection.execute(
                    """
                    INSERT INTO analysis.ticker_input_manifest (
                        ticker_decision_id, field, source_id, source_version,
                        event_at, published_at, available_at, received_at, revision,
                        license, original_value, revised_value
                    ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    [
                        decision_id, field, source,
                        str(row.get("source_version") or row.get("version") or "") or None,
                        _parse_datetime(row.get("event_at") or row.get("event_time")),
                        _parse_datetime(row.get("published_at") or row.get("publication_time")),
                        available,
                        _parse_datetime(row.get("received_at") or row.get("receipt_time")),
                        str(row.get("revision") or "") or None,
                        str(row.get("license") or "") or None,
                        Jsonb(_jsonable(row.get("original_value") if row.get("original_value") is not None else row)),
                        Jsonb(_jsonable(row.get("revised_value") if row.get("revised_value") is not None else row)),
                    ],
                )

    def _evaluate(self, decision: dict[str, Any], horizon: Horizon, sessions: int, reference: datetime) -> dict[str, Any]:
        with self.runtime.read(JOB_PROFILE) as connection:
            entry_quote = connection.execute(
                """
                SELECT price, observed_at, available_at
                FROM raw.confirmed_quote
                WHERE instrument_id = %s
                  AND observed_at <= %s AND available_at <= %s
                ORDER BY observed_at DESC, available_at DESC
                LIMIT 1
                """,
                [decision["instrument_id"], decision["as_of"], decision["as_of"]],
            ).fetchone()
            entry = connection.execute(
                """
                SELECT close, available_at, observed_at
                FROM raw.confirmed_price_bar
                WHERE instrument_id = %s AND interval = '1d'
                  AND trading_date <= %s::date AND available_at <= %s
                ORDER BY trading_date DESC, available_at DESC LIMIT 1
                """,
                [decision["instrument_id"], decision["as_of"], decision["as_of"]],
            ).fetchone()
            sector = connection.execute(
                "SELECT sector FROM catalog.instrument WHERE id = %s",
                [decision["instrument_id"]],
            ).fetchone()
            benchmark = connection.execute(
                """
                SELECT exact_membership
                FROM analysis.ticker_benchmark_snapshot
                WHERE benchmark_key = 'market-equity-etf' AND available_at <= %s
                ORDER BY as_of DESC LIMIT 1
                """,
                [decision["as_of"]],
            ).fetchone()
            marks = connection.execute(
                """
                SELECT close, available_at, observed_at, trading_date
                FROM raw.confirmed_price_bar
                WHERE instrument_id = %s AND interval = '1d'
                  AND trading_date > %s::date AND available_at <= %s
                ORDER BY trading_date, available_at
                LIMIT %s
                """,
                [decision["instrument_id"], decision["as_of"], reference, sessions],
            ).fetchall()
        if entry is None or not marks:
            return {
                "state": "unmeasurable", "available_at": None,
                "selected_return": None, "stock_return": None,
                "sector_return": None, "market_return": None,
                "error_type": None, "mistake_card": {},
            }
        entry_price = float(entry_quote["price"]) if entry_quote is not None else float(entry["close"])
        entry_date = entry_quote["observed_at"].date() if entry_quote is not None else entry["observed_at"].date()
        mark = marks[-1]
        stock_return = float(mark["close"]) / entry_price - 1
        sector_symbols = self._peer_symbols(sector["sector"] if sector else None)
        market_members = list((benchmark or {}).get("exact_membership") or []) if benchmark else []
        sector_return = self._peer_return(
            sector_symbols, entry_date, mark["trading_date"], decision["as_of"], reference,
        )
        market_return = self._peer_return(
            market_members or ["SPY", "QQQ"], entry_date, mark["trading_date"], decision["as_of"], reference,
        )
        fundamental = dict(decision["fundamental"] or {})
        stance = str(fundamental.get("stance") or "NEUTRAL")
        action = str((decision["capital_action"] or {}).get("action") or "")
        error = None
        if stance == "BULLISH" and stock_return < 0 or stance == "BEARISH" and stock_return > 0:
            error = "direction_error"
        if action == "AVOID" and stock_return > 0:
            error = "correct_avoidance_opportunity_cost"
        selected = dict(decision.get("selected_expression") or {})
        selected_kind = str(selected.get("kind") or "STOCK")
        selected_return = stock_return if selected_kind == "STOCK" else 0.0 if selected_kind == "CASH" else None
        return {
            "state": "resolved" if len(marks) >= sessions else "observing",
            "available_at": mark["available_at"],
            "selected_return": selected_return if action not in {"AVOID", "WAIT_FOR_PRICE"} else 0,
            "stock_return": stock_return,
            "sector_return": sector_return,
            "market_return": market_return,
            "error_type": error,
            "mistake_card": {
                "belief": fundamental.get("stance"),
                "observed_return": stock_return,
                "action": action,
            } if error else {},
        }

    def _peer_symbols(self, sector: Any) -> list[str]:
        if not sector:
            return []
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT symbol FROM catalog.instrument
                WHERE asset_class = 'equity' AND sector = %s
                ORDER BY symbol
                """,
                [sector],
            ).fetchall()
        return [str(row["symbol"]).upper() for row in rows if row["symbol"]]

    def _peer_return(
        self,
        symbols: list[str],
        entry_date: Any,
        mark_date: Any,
        as_of: datetime,
        reference: datetime,
    ) -> float | None:
        if not symbols:
            return None
        with self.runtime.read(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                WITH entry_prices AS (
                    SELECT DISTINCT ON (bar.instrument_id)
                           bar.instrument_id, bar.close
                    FROM raw.confirmed_price_bar bar
                    JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
                    WHERE instrument.symbol = ANY(%s)
                      AND bar.interval = '1d'
                      AND bar.trading_date <= %s
                      AND bar.available_at <= %s
                    ORDER BY bar.instrument_id, bar.trading_date DESC, bar.available_at DESC
                ), mark_prices AS (
                    SELECT DISTINCT ON (bar.instrument_id)
                           bar.instrument_id, bar.close
                    FROM raw.confirmed_price_bar bar
                    JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
                    WHERE instrument.symbol = ANY(%s)
                      AND bar.interval = '1d'
                      AND bar.trading_date = %s
                      AND bar.available_at <= %s
                    ORDER BY bar.instrument_id, bar.available_at DESC
                )
                SELECT avg(mark_prices.close / entry_prices.close - 1) AS return
                FROM entry_prices JOIN mark_prices USING (instrument_id)
                WHERE entry_prices.close > 0
                """,
                [symbols, entry_date, as_of, symbols, mark_date, reference],
            ).fetchone()
        return _number(row["return"]) if row else None

    def _store_outcome(
        self,
        decision_id: str,
        horizon: Horizon,
        sessions: int,
        outcome: dict[str, Any],
        *,
        selected_expression: str,
    ) -> None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """
                INSERT INTO analysis.ticker_outcome (
                    ticker_decision_id, horizon, horizon_sessions, state,
                    measured_through, selected_expression, selected_return,
                    stock_counterfactual_return, cash_return, sector_return,
                    market_return, error_type, mistake_card, available_at,
                    metadata, updated_at
                ) VALUES (
                    %s::uuid, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s, now()
                )
                ON CONFLICT (ticker_decision_id, horizon, horizon_sessions) DO UPDATE SET
                    state = EXCLUDED.state, measured_through = EXCLUDED.measured_through,
                    selected_return = EXCLUDED.selected_return,
                    stock_counterfactual_return = EXCLUDED.stock_counterfactual_return,
                    cash_return = EXCLUDED.cash_return, error_type = EXCLUDED.error_type,
                    sector_return = EXCLUDED.sector_return, market_return = EXCLUDED.market_return,
                    mistake_card = EXCLUDED.mistake_card, available_at = EXCLUDED.available_at,
                    metadata = EXCLUDED.metadata, updated_at = now()
                """,
                [
                    decision_id, horizon.value, sessions, outcome["state"],
                    outcome["available_at"], selected_expression,
                    outcome["selected_return"], outcome["stock_return"],
                    outcome.get("sector_return"), outcome.get("market_return"),
                    outcome["error_type"], Jsonb(outcome["mistake_card"]),
                    outcome["available_at"], Jsonb({
                        "episode_unit": "ticker_decision",
                        "selected_expression": selected_expression,
                    }),
                ],
            )


def _decision_from_row(row: Any) -> TickerDecision:
    return TickerDecision.model_validate({
        "decision_contract_version": row["contract_version"],
        "ticker": row["ticker"],
        "as_of": row["as_of"],
        "decision_revision": row["decision_revision"],
        "tactical": row["tactical"],
        "fundamental": row["fundamental"],
        "capital_action": row["capital_action"],
        "risk_policy": row["risk_policy"],
        "expressions": row["expressions"],
        "selected_expression": row["selected_expression"],
        "data_requests": row["data_requests"],
        "learning_history": row["learning_history"],
        "input_manifest": row["input_manifest"],
    })


def _first_statement(values: Any) -> str | None:
    if not isinstance(values, list) or not values:
        return None
    first = values[0]
    return str(first.get("statement") or "") if isinstance(first, dict) else str(first)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


__all__ = ["HORIZON_SESSIONS", "TickerDecisionRepository"]
