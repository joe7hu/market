"""Persistence and learning reads for versioned ticker decisions."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Iterable
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.core.decision import (
    Horizon,
    TickerDecision,
    capital_action_from_resolution,
    evaluate_ticker_policy,
    resolution_from_legacy,
)
from investment_panel.core.options_recovery import FEE_PER_CONTRACT_LEG
from investment_panel.database.options_paper_quotes import is_credit_structure, package_price
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


HORIZON_SESSIONS = {
    Horizon.TACTICAL: (1, 5, 20),
    Horizon.FUNDAMENTAL: (63, 126, 252),
}
STOCK_COST_MODEL_VERSION = "stock-close-estimated-cost-v1"
STOCK_COST_PER_SIDE_BPS = 10.0


class TickerDecisionRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def publish(self, decision: TickerDecision) -> dict[str, Any]:
        """Publish an immutable decision revision and its input manifest."""

        payload = decision.model_dump(mode="json")
        payload["input_manifest"] = {
            **dict(payload.get("input_manifest") or {}),
            "instrument_state_snapshot": payload.get("instrument_state_snapshot"),
            "alpha_signals": payload.get("alpha_signals") or [],
            "opportunity_rank": payload.get("opportunity_rank"),
        }
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
                    tactical, fundamental, capital_action, resolution, policy_version,
                    opportunity_episode_id, opportunity_cutoff, opportunity_episode, risk_policy,
                    expressions, selected_expression, data_requests,
                    learning_history, input_manifest, market_state_publication_id,
                    market_state_snapshot, portfolio_impacts, risk_policy_snapshot, status
                ) VALUES (
                    %s, %s, %s, %s, now(), %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, 'published'
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
                    Jsonb(payload["capital_action"]), Jsonb(payload["resolution"]),
                    decision.policy_version, decision.opportunity_episode.episode_id,
                    decision.opportunity_episode.cutoff,
                    Jsonb(payload["opportunity_episode"]), Jsonb(payload["risk_policy"]),
                    Jsonb(payload["expressions"]), Jsonb(payload.get("selected_expression")),
                    Jsonb(payload["data_requests"]), Jsonb(payload["learning_history"]),
                    Jsonb(payload["input_manifest"]),
                    _uuid_or_none(decision.market_state_publication_id),
                    Jsonb(payload.get("market_state_snapshot") or {}),
                    Jsonb(payload.get("portfolio_impacts") or {}),
                    Jsonb(payload.get("risk_policy_snapshot") or {}),
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
                       decision.resolution, decision.policy_version,
                       decision.opportunity_episode_id, decision.opportunity_cutoff,
                       decision.opportunity_episode, decision.risk_policy, decision.expressions,
                       decision.selected_expression, decision.data_requests,
                       decision.learning_history, decision.input_manifest,
                       decision.market_state_publication_id::text,
                       decision.market_state_snapshot, decision.portfolio_impacts,
                       decision.risk_policy_snapshot
                FROM analysis.ticker_decision decision
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                WHERE instrument.symbol = %s AND decision.status = 'published'
                ORDER BY decision.as_of DESC, decision.created_at DESC
                LIMIT 1
                """,
                [ticker.strip().upper()],
            ).fetchone()
        if not row:
            return None
        try:
            return _decision_from_row(row)
        except (TypeError, ValueError, KeyError):
            # Legacy rows remain readable through the raw panel model, but a
            # malformed row must not block a new canonical publication.
            return None

    def refresh_outcomes(
        self,
        *,
        now: datetime | None = None,
        limit: int = 2_000,
        symbols: Iterable[str] | None = None,
        since: datetime | None = None,
    ) -> dict[str, int]:
        reference = _utc(now or datetime.now(UTC))
        selected = (
            None
            if symbols is None
            else sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
        )
        if selected == []:
            return {"evaluated": 0, "updated": 0, "resolved": 0}
        filters = ["decision.status IN ('published', 'superseded')"]
        parameters: list[Any] = []
        if selected is not None:
            filters.append("instrument.symbol = ANY(%s)")
            parameters.append(selected)
        if since is not None:
            filters.append("decision.as_of >= %s")
            parameters.append(_utc(since))
        parameters.append(max(1, min(int(limit), 10_000)))
        with self.runtime.read(JOB_PROFILE) as connection:
            decisions = connection.execute(
                f"""
                SELECT decision.id::text AS decision_id, instrument.id AS instrument_id,
                       instrument.symbol AS ticker, decision.as_of,
                       decision.tactical, decision.fundamental,
                       decision.capital_action, decision.expressions,
                       decision.selected_expression
                FROM analysis.ticker_decision decision
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                WHERE {" AND ".join(filters)}
                ORDER BY decision.as_of, decision.id
                LIMIT %s
                """,
                parameters,
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
                       outcome.error_type, outcome.mistake_card, outcome.metadata
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
            horizon_episodes = connection.execute(
                """
                SELECT count(DISTINCT (decision.id, outcome.horizon)) AS episodes
                FROM analysis.ticker_decision decision
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                JOIN analysis.ticker_outcome outcome ON outcome.ticker_decision_id = decision.id
                WHERE instrument.symbol = %s
                """,
                [ticker.strip().upper()],
            ).fetchone()["episodes"]
            policy_rows = connection.execute(
                """
                SELECT ticker_decision_id, ticker, as_of, horizon, state,
                       selected_return, stock_counterfactual_return, metadata, scenarios
                FROM (
                    SELECT decision.id::text AS ticker_decision_id, instrument.symbol AS ticker,
                           decision.as_of, outcome.horizon, outcome.horizon_sessions, outcome.state,
                           outcome.selected_return, outcome.stock_counterfactual_return,
                           outcome.metadata, decision.fundamental->'scenarios' AS scenarios,
                           row_number() OVER (
                               PARTITION BY decision.id, outcome.horizon
                               ORDER BY outcome.horizon_sessions DESC, outcome.updated_at DESC, outcome.id DESC
                           ) AS horizon_rank
                    FROM analysis.ticker_outcome outcome
                    JOIN analysis.ticker_decision decision ON decision.id = outcome.ticker_decision_id
                    JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                    WHERE outcome.state = 'resolved'
                ) ranked
                WHERE horizon_rank = 1
                ORDER BY as_of, ticker_decision_id, horizon
                LIMIT 10000
                """
            ).fetchall()
        strategy_learning = evaluate_ticker_policy(policy_rows)
        if not decision:
            return {
                "independent_episode_count": 0,
                "disagreement": {},
                "expression_tournament": [],
                "mistake_cards": [],
                "strategy_learning": strategy_learning,
            }
        tactical = dict(decision["tactical"] or {})
        fundamental = dict(decision["fundamental"] or {})
        expressions = dict(decision["expressions"] or {})
        tournament_outcomes = [dict(row) for row in outcomes]
        return {
            "independent_episode_count": int(episodes or 0),
            "independent_horizon_episode_count": int(horizon_episodes or 0),
            "effective_sample_count": int(horizon_episodes or 0),
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
                    "outcomes": [
                        {
                            **row,
                            "expression_return": (
                                dict(row.get("metadata") or {}).get("expression_returns") or {}
                            ).get(kind),
                        }
                        for row in tournament_outcomes
                    ],
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
            "strategy_learning": strategy_learning,
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
                available = _parse_datetime(row.get("available_at") or row.get("as_of"))
                if available is None or available > decision.as_of:
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
            trend_reference = connection.execute(
                """
                WITH one_bar_per_day AS (
                    SELECT DISTINCT ON (trading_date)
                           close, trading_date, observed_at, available_at, source_id
                    FROM raw.confirmed_price_bar
                    WHERE instrument_id = %s AND interval = '1d'
                      AND trading_date <= %s::date AND available_at <= %s
                    ORDER BY trading_date, available_at DESC, observed_at DESC, source_id
                )
                SELECT close, trading_date
                FROM one_bar_per_day
                ORDER BY trading_date DESC
                OFFSET 20 LIMIT 1
                """,
                [decision["instrument_id"], decision["as_of"], decision["as_of"]],
            ).fetchone()
            sector = connection.execute(
                """
                SELECT sector, delisted_at, delisting_price,
                       delisting_available_at, delisting_source
                FROM catalog.instrument
                WHERE id = %s
                """,
                [decision["instrument_id"]],
            ).fetchone()
            regime = connection.execute(
                """
                SELECT feature.trend_state, feature.volatility_state,
                       feature.as_of, feature.feature_version,
                       analysis_run.input_cutoff
                FROM analysis.symbol_feature feature
                JOIN analysis.run analysis_run ON analysis_run.id = feature.run_id
                WHERE feature.instrument_id = %s
                  AND feature.as_of <= %s
                  AND analysis_run.input_cutoff <= %s
                  AND feature.trend_state <> 'unavailable'
                  AND feature.data_quality_status <> 'unavailable'
                ORDER BY feature.as_of DESC, analysis_run.input_cutoff DESC, feature.id DESC
                LIMIT 1
                """,
                [decision["instrument_id"], decision["as_of"], decision["as_of"]],
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
                  AND (%s::timestamptz IS NULL OR trading_date <= %s::date)
                ORDER BY trading_date, available_at
                LIMIT %s
                """,
                [
                    decision["instrument_id"], decision["as_of"], reference,
                    sector["delisted_at"] if sector else None,
                    sector["delisted_at"] if sector else None,
                    sessions,
                ],
            ).fetchall()
            delisting_status = _delisting_status(sector, reference)
            terminal_mark = _terminal_delisting_mark(
                connection,
                instrument_id=decision["instrument_id"],
                lifecycle=sector,
                as_of=decision["as_of"],
                reference=reference,
            )
            if terminal_mark is not None and delisting_status == "delisted":
                delisting_status = "delisted_terminal"
        if entry is None or (not marks and terminal_mark is None):
            return {
                "state": "unmeasurable", "available_at": None,
                "selected_return": None, "stock_return": None,
                "alternate_counterfactual_return": None,
                "sector_return": None, "market_return": None,
                "cash_return": 0.0, "trend_counterfactual_return": None,
                "cost_adjusted_selected_return": None,
                "cost_adjusted_stock_counterfactual_return": None,
                "cost_adjusted_cash_return": 0.0,
                "expression_returns": {"CASH": 0.0}, "expression_marks": {},
                "error_type": None, "mistake_card": {},
                "learning_metadata": _learning_metadata(
                    as_of=decision["as_of"],
                    measured_through=decision["as_of"],
                    reference=reference,
                    sector=sector["sector"] if sector else None,
                    regime=regime,
                    delisting_status=delisting_status,
                    decision=decision,
                ),
            }
        entry_price = float(entry_quote["price"]) if entry_quote is not None else float(entry["close"])
        entry_date = entry_quote["observed_at"].date() if entry_quote is not None else entry["observed_at"].date()
        mark = terminal_mark or marks[-1]
        stock_return = float(mark["close"]) / entry_price - 1
        stock_cost_adjusted = _stock_cost_adjusted_return(stock_return)
        stock_mark = {
            "status": "delisted_terminal" if terminal_mark is not None else "estimated",
            "gross_return": stock_return,
            "cost_adjusted_return": stock_cost_adjusted,
            "cost_model_version": STOCK_COST_MODEL_VERSION,
            "cost_basis": "confirmed close with conservative round-trip execution allowance",
            "cost_per_side_bps": STOCK_COST_PER_SIDE_BPS,
            "evidence_state": "ESTIMATED",
            "entry_price": entry_price,
            "mark_price": float(mark["close"]),
        }
        trend_return = None
        if trend_reference is not None and float(trend_reference["close"] or 0) > 0:
            trend_return = stock_return if entry_price > float(trend_reference["close"]) else 0.0
        sector_symbols = self._peer_symbols(sector["sector"] if sector else None)
        market_members = list((benchmark or {}).get("exact_membership") or []) if benchmark else []
        sector_return = self._peer_return(
            sector_symbols, entry_date, mark["trading_date"], decision["as_of"], reference,
        )
        market_return = self._peer_return(
            market_members or ["SPY", "QQQ"], entry_date, mark["trading_date"], decision["as_of"], reference,
        )
        expression_returns, expression_marks = self._expression_returns(
            decision=decision,
            as_of=decision["as_of"],
            mark_date=mark["trading_date"],
            reference=reference,
        )
        expression_returns["STOCK"] = stock_return
        expression_marks["STOCK"] = stock_mark
        expression_marks["CASH"] = {
            "status": "measured",
            "gross_return": 0.0,
            "cost_adjusted_return": 0.0,
            "cost_model_version": "cash-zero-cost-v1",
            "evidence_state": "DERIVED",
        }
        fundamental = dict(decision["fundamental"] or {})
        stance = str(fundamental.get("stance") or "NEUTRAL")
        action = str((decision["capital_action"] or {}).get("action") or "")
        selected = dict(decision.get("selected_expression") or {})
        selected_kind = str(selected.get("kind") or "STOCK")
        selected_return = expression_returns.get(selected_kind)
        if action in {"AVOID", "WAIT_FOR_PRICE"}:
            selected_return = 0.0
        expression_costs = {
            kind: _number(metadata.get("cost_adjusted_return"))
            for kind, metadata in expression_marks.items()
            if _number(metadata.get("cost_adjusted_return")) is not None
        }
        cost_adjusted_selected = 0.0 if action in {"AVOID", "WAIT_FOR_PRICE"} else expression_costs.get(selected_kind)
        preferred_view = fundamental if stance != "NEUTRAL" else dict(decision["tactical"] or {})
        alternate_kind = str(preferred_view.get("alternate_expression") or "CASH")
        state = "resolved" if terminal_mark is not None or len(marks) >= sessions else "observing"
        error: str | None = None
        mistake_card: dict[str, Any] = {}
        if state == "resolved":
            error, mistake_card = _classify_mistake(
                stance=stance,
                action=action,
                selected_kind=selected_kind,
                selected_return=selected_return,
                stock_return=stock_return,
                alternate_return=expression_returns.get(alternate_kind),
            )
        return {
            "state": state,
            "available_at": mark["available_at"],
            "selected_return": selected_return,
            "stock_return": stock_return,
            "cash_return": 0.0,
            "trend_counterfactual_return": trend_return,
            "cost_adjusted_selected_return": cost_adjusted_selected,
            "cost_adjusted_stock_counterfactual_return": stock_cost_adjusted,
            "cost_adjusted_cash_return": 0.0,
            "alternate_counterfactual_return": expression_returns.get(alternate_kind),
            "sector_return": sector_return,
            "market_return": market_return,
            "expression_returns": expression_returns,
            "expression_marks": expression_marks,
            "alternate_expression": alternate_kind,
            "learning_metadata": _learning_metadata(
                as_of=decision["as_of"],
                measured_through=mark["observed_at"],
                reference=reference,
                sector=sector["sector"] if sector else None,
                regime=regime,
                delisting_status=delisting_status,
                decision=decision,
            ),
            "error_type": error,
            "mistake_card": mistake_card,
        }

    def _expression_returns(
        self,
        *,
        decision: dict[str, Any],
        as_of: datetime,
        mark_date: Any,
        reference: datetime,
    ) -> tuple[dict[str, float | None], dict[str, dict[str, Any]]]:
        """Evaluate options against later executable quotes for this ticker."""

        expressions = dict(decision.get("expressions") or {})
        returns: dict[str, float | None] = {"CASH": 0.0}
        marks: dict[str, dict[str, Any]] = {}
        with self.runtime.read(JOB_PROFILE) as connection:
            for raw_kind, raw_expression in expressions.items():
                kind = str(raw_kind)
                if kind in {"STOCK", "CASH"}:
                    continue
                value, metadata = self._option_expression_return(
                    connection,
                    dict(raw_expression or {}),
                    as_of=as_of,
                    mark_date=mark_date,
                    reference=reference,
                )
                returns[kind] = value
                marks[kind] = metadata
        return returns, marks

    def _option_expression_return(
        self,
        connection: Any,
        expression: dict[str, Any],
        *,
        as_of: datetime,
        mark_date: Any,
        reference: datetime,
    ) -> tuple[float | None, dict[str, Any]]:
        """Use the next feasible quote package; never use a midpoint fallback."""

        legs = [dict(leg) for leg in expression.get("legs") or [] if isinstance(leg, dict)]
        contract_ids: list[int] = []
        for leg in legs:
            try:
                contract_ids.append(int(str(leg.get("contract_id") or "")))
            except (TypeError, ValueError):
                return None, {"status": "unmeasurable", "reason": "contract_id_missing"}
        if not legs or len(contract_ids) != len(legs):
            return None, {"status": "unmeasurable", "reason": "option_legs_missing"}
        entry_package = package_price(legs, phase="entry")
        if entry_package is None or entry_package <= 0:
            return None, {"status": "unmeasurable", "reason": "entry_executable_quote_missing"}
        rows = connection.execute(
            """
            WITH valid_quotes AS (
                SELECT DISTINCT ON (quote.snapshot_id, quote.contract_id)
                       quote.snapshot_id, quote.contract_id, quote.bid, quote.ask,
                       quote.bid_size, quote.ask_size, quote.observed_at,
                       quote.available_at, contract.multiplier
                FROM raw.option_quote quote
                JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
                JOIN catalog.option_contract contract ON contract.id = quote.contract_id
                WHERE quote.contract_id = ANY(%s::bigint[])
                  AND quote.observed_at > %s
                  AND quote.observed_at::date <= %s
                  AND quote.available_at <= %s
                  AND snapshot.capture_state IN ('complete', 'partial')
                  AND quote.bid IS NOT NULL AND quote.ask IS NOT NULL
                  AND quote.bid > 0 AND quote.ask >= quote.bid
                ORDER BY quote.snapshot_id, quote.contract_id,
                         quote.observed_at DESC, quote.available_at DESC, quote.id DESC
            ), complete_snapshots AS (
                SELECT snapshot_id, max(observed_at) AS observed_at,
                       max(available_at) AS available_at
                FROM valid_quotes
                GROUP BY snapshot_id
                HAVING count(DISTINCT contract_id) = %s
            )
            SELECT valid_quotes.*
            FROM valid_quotes
            JOIN complete_snapshots USING (snapshot_id)
            ORDER BY complete_snapshots.observed_at DESC,
                     complete_snapshots.available_at DESC, valid_quotes.contract_id
            """,
            [contract_ids, as_of, mark_date, reference, len(contract_ids)],
        ).fetchall()
        snapshots: dict[Any, dict[int, Any]] = {}
        for row in rows:
            snapshots.setdefault(row["snapshot_id"], {})[int(row["contract_id"])] = row
        complete = [
            by_contract
            for by_contract in snapshots.values()
            if len(by_contract) == len(contract_ids)
        ]
        if not complete:
            return None, {
                "status": "unmeasurable",
                "reason": "next_feasible_option_quote_missing",
                "contracts_found": sorted({int(row["contract_id"]) for row in rows}),
            }
        by_contract = complete[0]
        mark_legs = [
            {
                **leg,
                "bid": by_contract[int(leg["contract_id"])]["bid"],
                "ask": by_contract[int(leg["contract_id"])]["ask"],
                "bid_size": by_contract[int(leg["contract_id"])]["bid_size"],
                "ask_size": by_contract[int(leg["contract_id"])]["ask_size"],
            }
            for leg in legs
        ]
        mark_package = package_price(mark_legs, phase="exit")
        if mark_package is None:
            return None, {"status": "unmeasurable", "reason": "exit_executable_quote_missing"}
        max_loss = _number(expression.get("max_loss_per_unit"))
        multiplier = max(1, int(by_contract[next(iter(by_contract))]["multiplier"] or 100))
        denominator = max_loss if max_loss and max_loss > 0 else entry_package * multiplier
        kind = str(expression.get("kind") or "").lower()
        gross_pnl = (
            (entry_package - mark_package) * multiplier
            if is_credit_structure(kind)
            else (mark_package - entry_package) * multiplier
        )
        fees = FEE_PER_CONTRACT_LEG * len(legs) * 2
        return float(gross_pnl / denominator), {
            "status": "measured",
            "entry_package": entry_package,
            "mark_package": mark_package,
            "multiplier": multiplier,
            "gross_return": gross_pnl / denominator,
            "fees": fees,
            "cost_adjusted_return": (gross_pnl - fees) / denominator,
            "cost_model_version": "option-executable-quotes-fees-v1",
            "mark_quote_time": max(row["observed_at"] for row in by_contract.values()),
            "mark_available_at": max(row["available_at"] for row in by_contract.values()),
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
                    stock_counterfactual_return, alternate_counterfactual_return,
                    cash_return, sector_return,
                    market_return, error_type, mistake_card, available_at,
                    metadata, updated_at
                ) VALUES (
                    %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()
                )
                ON CONFLICT (ticker_decision_id, horizon, horizon_sessions) DO UPDATE SET
                    state = EXCLUDED.state, measured_through = EXCLUDED.measured_through,
                    selected_return = EXCLUDED.selected_return,
                    stock_counterfactual_return = EXCLUDED.stock_counterfactual_return,
                    alternate_counterfactual_return = EXCLUDED.alternate_counterfactual_return,
                    cash_return = EXCLUDED.cash_return, error_type = EXCLUDED.error_type,
                    sector_return = EXCLUDED.sector_return, market_return = EXCLUDED.market_return,
                    mistake_card = EXCLUDED.mistake_card, available_at = EXCLUDED.available_at,
                    metadata = EXCLUDED.metadata, updated_at = now()
                """,
                [
                    decision_id, horizon.value, sessions, outcome["state"],
                    outcome["available_at"], selected_expression,
                    outcome["selected_return"], outcome["stock_return"],
                    outcome.get("alternate_counterfactual_return"),
                    outcome.get("cash_return", 0.0),
                    outcome.get("sector_return"), outcome.get("market_return"),
                    outcome["error_type"], Jsonb(outcome["mistake_card"]),
                    outcome["available_at"], Jsonb({
                        "episode_unit": "ticker_decision",
                        "selected_expression": selected_expression,
                        "alternate_expression": outcome.get("alternate_expression"),
                        "expression_returns": _jsonable(outcome.get("expression_returns") or {}),
                        "expression_marks": _jsonable(outcome.get("expression_marks") or {}),
                        "trend_counterfactual_return": outcome.get("trend_counterfactual_return"),
                        "cost_adjusted_selected_return": outcome.get("cost_adjusted_selected_return"),
                        "cost_adjusted_stock_counterfactual_return": outcome.get("cost_adjusted_stock_counterfactual_return"),
                        "cost_adjusted_cash_return": outcome.get("cost_adjusted_cash_return", 0.0),
                        "cost_model_version": outcome.get("cost_model_version") or "mixed-expression-cost-model-v1",
                        **dict(outcome.get("learning_metadata") or {}),
                    }),
                ],
            )


def _decision_from_row(row: Any) -> TickerDecision:
    resolution = resolution_from_legacy(dict(row))
    manifest = dict(row["input_manifest"] or {})
    return TickerDecision.model_validate({
        "decision_contract_version": row["contract_version"],
        "ticker": row["ticker"],
        "as_of": row["as_of"],
        "decision_revision": row["decision_revision"],
        "tactical": row["tactical"],
        "fundamental": row["fundamental"],
        "capital_action": capital_action_from_resolution(resolution),
        "resolution": resolution,
        "policy_version": (row.get("policy_version") if hasattr(row, "get") else row["policy_version"])
            or resolution.policy_version,
        "risk_policy": row["risk_policy"],
        "expressions": row["expressions"],
        "selected_expression": row["selected_expression"],
        "data_requests": row["data_requests"],
        "learning_history": row["learning_history"],
        "input_manifest": manifest,
        "market_state_publication_id": row.get("market_state_publication_id") if hasattr(row, "get") else None,
        "market_state_snapshot": row.get("market_state_snapshot") if hasattr(row, "get") else None,
        "portfolio_impacts": row.get("portfolio_impacts") if hasattr(row, "get") else {},
        "risk_policy_snapshot": row.get("risk_policy_snapshot") if hasattr(row, "get") else None,
        "opportunity_episode": (
            row.get("opportunity_episode") if hasattr(row, "get") else None
        ) or None,
        "instrument_state_snapshot": manifest.get("instrument_state_snapshot"),
        "alpha_signals": manifest.get("alpha_signals") or [],
        "opportunity_rank": manifest.get("opportunity_rank"),
    })


def _uuid_or_none(value: Any) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _first_statement(values: Any) -> str | None:
    if not isinstance(values, list) or not values:
        return None
    first = values[0]
    return str(first.get("statement") or "") if isinstance(first, dict) else str(first)


def _classify_mistake(
    *,
    stance: str,
    action: str,
    selected_kind: str,
    selected_return: float | None,
    stock_return: float,
    alternate_return: float | None,
) -> tuple[str | None, dict[str, Any]]:
    """Classify one deterministic episode without treating missing marks as loss."""

    if action == "AVOID" and stock_return > 0:
        return "correct_avoidance_opportunity_cost", {
            "belief": stance,
            "action": action,
            "observed_stock_return": stock_return,
            "proposed_rule_change": "Retain avoidance only when the named bearish invalidation or catalyst risk remains active.",
        }
    if stance == "BULLISH" and stock_return < 0 or stance == "BEARISH" and stock_return > 0:
        return "direction_error", {
            "belief": stance,
            "action": action,
            "observed_stock_return": stock_return,
            "proposed_rule_change": "Re-test the directional evidence against sector, market, and catalyst baselines.",
        }
    if selected_kind not in {"STOCK", "CASH"} and selected_return is None:
        return "correct_thesis_with_untradeable_expression", {
            "belief": stance,
            "action": action,
            "selected_expression": selected_kind,
            "observed_stock_return": stock_return,
            "proposed_rule_change": "Require a later executable option package before selecting an option expression.",
        }
    if selected_kind not in {"STOCK", "CASH"} and selected_return is not None and stock_return > selected_return + 0.05:
        return "stock_versus_option_expression_error", {
            "belief": stance,
            "action": action,
            "selected_expression": selected_kind,
            "selected_return": selected_return,
            "stock_return": stock_return,
            "alternate_return": alternate_return,
            "proposed_rule_change": "Prefer stock when the slower thesis has good upside participation and option decay dominates.",
        }
    return None, {}


def _stock_cost_adjusted_return(gross_return: float) -> float:
    """Apply a visible, conservative estimated round-trip stock cost."""

    one_side = STOCK_COST_PER_SIDE_BPS / 10_000.0
    return (1.0 + gross_return) * (1.0 - one_side) ** 2 - 1.0


def _delisting_status(lifecycle: Any, reference: datetime) -> str:
    if not lifecycle:
        return "lifecycle_missing"
    delisted_at = lifecycle.get("delisted_at")
    if delisted_at is None or _utc(delisted_at) > _utc(reference):
        return "active"
    return "delisted"


def _terminal_delisting_mark(
    connection: Any,
    *,
    instrument_id: int,
    lifecycle: Any,
    as_of: datetime,
    reference: datetime,
) -> dict[str, Any] | None:
    """Return a point-in-time terminal mark for an explicitly delisted ticker."""

    if not lifecycle or lifecycle.get("delisted_at") is None:
        return None
    delisted_at = _utc(lifecycle["delisted_at"])
    if delisted_at <= _utc(as_of) or delisted_at > _utc(reference):
        return None
    price = _number(lifecycle.get("delisting_price"))
    available_at = lifecycle.get("delisting_available_at")
    if price is not None and price > 0 and available_at is not None and _utc(available_at) <= _utc(reference):
        return {
            "close": price,
            "available_at": available_at,
            "observed_at": delisted_at,
            "trading_date": delisted_at.date(),
        }
    row = connection.execute(
        """
        SELECT close, available_at, observed_at, trading_date
        FROM raw.confirmed_price_bar
        WHERE instrument_id = %s AND interval = '1d'
          AND trading_date > %s::date
          AND trading_date <= %s::date
          AND available_at <= %s
        ORDER BY trading_date DESC, available_at DESC, observed_at DESC
        LIMIT 1
        """,
        [instrument_id, as_of, delisted_at, reference],
    ).fetchone()
    return dict(row) if row is not None else None


def _learning_metadata(
    *,
    as_of: datetime,
    measured_through: datetime,
    reference: datetime,
    sector: Any,
    regime: Any,
    delisting_status: str,
    decision: dict[str, Any],
) -> dict[str, Any]:
    start = _utc(as_of).date()
    end = _utc(measured_through).date()
    age_days = max(0, (_utc(reference).date() - start).days)
    sample = "canary" if age_days <= 30 else "forward" if age_days <= 120 else "historical"
    tactical = dict(decision.get("tactical") or {})
    fundamental = dict(decision.get("fundamental") or {})
    decision_regime = (
        tactical.get("market_regime")
        or fundamental.get("market_regime")
        or tactical.get("regime")
        or fundamental.get("regime")
    )
    regime_row = dict(regime or {})
    regime_slice = str(decision_regime or "").strip()
    if not regime_slice:
        trend_state = str(regime_row.get("trend_state") or "").strip().lower()
        volatility_state = str(regime_row.get("volatility_state") or "").strip().lower()
        if trend_state in {"trend_up", "trend_down", "range", "transition"} and volatility_state in {
            "low", "normal", "high", "unstable",
        }:
            regime_slice = f"{trend_state}:{volatility_state}"
    regime_slice = regime_slice or "unknown"
    sector_slice = str(sector or "").strip() or "unknown"
    return {
        "sample": sample,
        # The disjoint policy samples are partitioned by decision-origin date.
        # Keep the realized outcome interval separately; using it as the
        # split interval makes rolling online episodes overlap by construction.
        "sample_start": start.isoformat(),
        "sample_end": start.isoformat(),
        "sample_definition": "decision-origin-age-windows-v2",
        "outcome_start": min(start, end).isoformat(),
        "outcome_end": max(start, end).isoformat(),
        "delistings_handled": delisting_status in {"active", "delisted_terminal"},
        "delisting_status": delisting_status,
        "sector_slice": sector_slice,
        "regime_slice": regime_slice,
        # Outcome refresh is an online, forward-only evaluator. No outcome is
        # used before its decision as_of, so the purge/embargo condition is
        # satisfied without claiming a backtest fit that did not occur.
        "purge_embargo_verified": _utc(measured_through) > _utc(as_of),
        "purge_embargo_policy": "online-forward-only-no-fit-v1",
        # One deterministic active policy is evaluated here. There is no
        # hidden model sweep whose best result would need correction.
        "multiple_trial_correction": "single-policy-no-trial-selection-v1",
        "point_in_time_defect": False,
        "outcome_selector": "confirmed_price_bar-available_at-v1",
    }


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
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


__all__ = ["HORIZON_SESSIONS", "TickerDecisionRepository"]
