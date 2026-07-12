"""Reproducible analysis runs and atomic PostgreSQL publications."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from typing import Any, Mapping, Sequence
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


class AnalysisRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def register_strategy(
        self,
        strategy_key: str,
        revision: int,
        *,
        name: str,
        parameters: Mapping[str, Any],
        status: str = "candidate",
        supersedes_id: int | None = None,
        authority_group: str | None = None,
    ) -> int:
        with self.runtime.transaction() as connection:
            row = connection.execute(
                f"""
                INSERT INTO analysis.strategy_revision
                    (strategy_key, revision, name, status, parameters, supersedes_id, authority_group,
                     promoted_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s = 'active' THEN now() ELSE NULL END)
                ON CONFLICT (strategy_key, revision) DO UPDATE
                SET name = EXCLUDED.name, parameters = EXCLUDED.parameters
                RETURNING id
                """,
                [
                    strategy_key, revision, name, status, Jsonb(dict(parameters)), supersedes_id,
                    authority_group or strategy_key, status,
                ],
            ).fetchone()
        return int(row["id"])

    def start_run(
        self,
        run_type: str,
        *,
        input_cutoff: datetime,
        code_version: str,
        inputs: Mapping[str, Any],
        feature_versions: Mapping[str, str] | None = None,
        strategy_revision_id: int | None = None,
    ) -> UUID:
        if input_cutoff.tzinfo is None:
            raise ValueError("input_cutoff must be timezone-aware")
        input_hash = _hash(inputs)
        with self.runtime.transaction() as connection:
            row = connection.execute(
                """
                INSERT INTO analysis.run
                    (run_type, input_cutoff, code_version, feature_versions,
                     strategy_revision_id, input_hash, started_at, status)
                VALUES (%s, %s, %s, %s, %s, %s, now(), 'running')
                RETURNING id
                """,
                [run_type, input_cutoff, code_version, Jsonb(dict(feature_versions or {})), strategy_revision_id, input_hash],
            ).fetchone()
        return UUID(str(row["id"]))

    def finish_run(self, run_id: UUID, status: str, summary: Mapping[str, Any] | None = None) -> None:
        if status not in {"succeeded", "partial", "failed"}:
            raise ValueError("analysis status is invalid")
        with self.runtime.transaction() as connection:
            result = connection.execute(
                """
                UPDATE analysis.run SET status = %s, finished_at = now(), summary = summary || %s
                WHERE id = %s AND status = 'running'
                """,
                [status, Jsonb(dict(summary or {})), run_id],
            )
            if result.rowcount != 1:
                raise ValueError(f"analysis run is not running: {run_id}")

    def store_option_feature(
        self,
        run_id: UUID,
        *,
        snapshot_id: int,
        contract_id: int,
        quote_observed_at: datetime,
        feature_version: str,
        values: Mapping[str, Any],
    ) -> int:
        columns = (
            "modeled_iv", "modeled_delta", "modeled_gamma", "modeled_theta", "modeled_vega",
            "dte", "spread_pct", "iv_rank", "iv_percentile", "liquidity_score", "flow_score",
            "convexity_score", "required_2x_price", "required_5x_price", "required_10x_price",
            "required_move_pct",
        )
        with self.runtime.transaction(JOB_PROFILE) as connection:
            row = connection.execute(
                f"""
                INSERT INTO analysis.option_feature
                    (run_id, snapshot_id, contract_id, quote_observed_at, feature_version,
                     {', '.join(columns)}, ev_inputs, metrics)
                VALUES (%s, %s, %s, %s, %s, {', '.join(['%s'] * len(columns))}, %s, %s)
                ON CONFLICT (run_id, snapshot_id, contract_id, feature_version) DO UPDATE
                SET metrics = EXCLUDED.metrics, ev_inputs = EXCLUDED.ev_inputs
                RETURNING id
                """,
                [
                    run_id, snapshot_id, contract_id, quote_observed_at, feature_version,
                    *(values.get(column) for column in columns),
                    Jsonb(dict(values.get("ev_inputs") or {})), Jsonb(dict(values.get("metrics") or {})),
                ],
            ).fetchone()
        return int(row["id"])

    def store_option_decision(
        self,
        run_id: UUID,
        *,
        decision_key: str,
        instrument_id: int,
        contract_id: int,
        snapshot_id: int,
        quote_observed_at: datetime,
        state: str,
        score: float | None,
        rank: int | None,
        inputs: Mapping[str, Any],
        reasons: Sequence[str] = (),
        blockers: Sequence[str] = (),
        details: Mapping[str, Any] | None = None,
        strategy_revision_id: int | None = None,
    ) -> UUID:
        option = dict(details or {})
        with self.runtime.transaction(JOB_PROFILE) as connection:
            decision = connection.execute(
                """
                INSERT INTO analysis.decision
                    (run_id, decision_key, kind, instrument_id, as_of, state, rank, score,
                     quality_status, strategy_revision_id, reasons, blockers, input_hash)
                VALUES (%s, %s, 'option', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, decision_key) DO UPDATE
                SET state = EXCLUDED.state, rank = EXCLUDED.rank, score = EXCLUDED.score,
                    quality_status = EXCLUDED.quality_status, reasons = EXCLUDED.reasons,
                    blockers = EXCLUDED.blockers, input_hash = EXCLUDED.input_hash
                RETURNING id
                """,
                [
                    run_id, decision_key, instrument_id, quote_observed_at, state, rank, score,
                    option.get("quality_status"), strategy_revision_id, list(reasons), list(blockers), _hash(inputs),
                ],
            ).fetchone()
            decision_id = UUID(str(decision["id"]))
            connection.execute(
                f"""
                INSERT INTO analysis.option_decision
                    (decision_id, contract_id, snapshot_id, quote_observed_at, premium_mid,
                     fill_assumption, required_move_pct, buy_under, predicted_p2x,
                     predicted_p5x, ev_multiple, tier, synthetic_legs, structure,
                     entry_price, exit_cost_estimate, secured_cash, max_profit, max_loss,
                     break_even, effective_assignment_price, probability_profit,
                     probability_assignment, probability_touch, expected_value,
                     risk_adjusted_expectancy, tail_cvar, data_confidence,
                     execution_confidence, details)
                VALUES ({', '.join(['%s'] * 30)})
                ON CONFLICT (decision_id) DO UPDATE
                SET premium_mid = EXCLUDED.premium_mid, fill_assumption = EXCLUDED.fill_assumption,
                    required_move_pct = EXCLUDED.required_move_pct, buy_under = EXCLUDED.buy_under,
                    predicted_p2x = EXCLUDED.predicted_p2x, predicted_p5x = EXCLUDED.predicted_p5x,
                    ev_multiple = EXCLUDED.ev_multiple, tier = EXCLUDED.tier,
                    synthetic_legs = EXCLUDED.synthetic_legs,
                    structure = EXCLUDED.structure, entry_price = EXCLUDED.entry_price,
                    exit_cost_estimate = EXCLUDED.exit_cost_estimate,
                    secured_cash = EXCLUDED.secured_cash, max_profit = EXCLUDED.max_profit,
                    max_loss = EXCLUDED.max_loss, break_even = EXCLUDED.break_even,
                    effective_assignment_price = EXCLUDED.effective_assignment_price,
                    probability_profit = EXCLUDED.probability_profit,
                    probability_assignment = EXCLUDED.probability_assignment,
                    probability_touch = EXCLUDED.probability_touch,
                    expected_value = EXCLUDED.expected_value,
                    risk_adjusted_expectancy = EXCLUDED.risk_adjusted_expectancy,
                    tail_cvar = EXCLUDED.tail_cvar, data_confidence = EXCLUDED.data_confidence,
                    execution_confidence = EXCLUDED.execution_confidence,
                    details = EXCLUDED.details
                """,
                [
                    decision_id, contract_id, snapshot_id, quote_observed_at,
                    option.get("premium_mid"), option.get("fill_assumption"), option.get("required_move_pct"),
                    option.get("buy_under"), option.get("predicted_p2x"), option.get("predicted_p5x"),
                    option.get("ev_multiple"), option.get("tier"), Jsonb(list(option.get("synthetic_legs") or [])),
                    option.get("structure") or "long_option", option.get("entry_price"),
                    option.get("exit_cost_estimate"), option.get("secured_cash"),
                    option.get("max_profit"), option.get("max_loss"), option.get("break_even"),
                    option.get("effective_assignment_price"), option.get("probability_profit"),
                    option.get("probability_assignment"), option.get("probability_touch"),
                    option.get("expected_value"), option.get("risk_adjusted_expectancy"),
                    option.get("tail_cvar"), option.get("data_confidence"),
                    option.get("execution_confidence"), Jsonb(dict(option.get("details") or {})),
                ],
            )
        return decision_id

    def publish(
        self,
        run_id: UUID,
        scope: str,
        models: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        validation: Mapping[str, Any] | None = None,
        complete_run_summary: Mapping[str, Any] | None = None,
        strategy_root_key: str | None = None,
    ) -> UUID:
        prepared = _prepare_models(models)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"publication:{scope}"])
            if strategy_root_key:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    [f"strategy:{strategy_root_key}"],
                )
            run = connection.execute(
                "SELECT status, strategy_revision_id FROM analysis.run WHERE id = %s FOR UPDATE",
                [run_id],
            ).fetchone()
            if run is None or run["status"] == "failed":
                raise ValueError("publication requires a non-failed analysis run")
            if strategy_root_key:
                active = connection.execute(
                    "SELECT id FROM analysis.strategy_revision "
                    "WHERE authority_group = %s AND status = 'active' FOR UPDATE",
                    [strategy_root_key],
                ).fetchall()
                if len(active) != 1 or active[0]["id"] != run["strategy_revision_id"]:
                    raise ValueError(
                        "strategy authority changed during analysis; publication must be recomputed"
                    )
            publication = connection.execute(
                """
                INSERT INTO app.publication (scope, analysis_run_id, status, validation)
                VALUES (%s, %s, 'building', %s) RETURNING id
                """,
                [scope, run_id, Jsonb(dict(validation or {}))],
            ).fetchone()
            publication_id = UUID(str(publication["id"]))
            for model_name, rows in prepared.items():
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO app.publication_item
                            (publication_id, model_name, stable_key, rank, instrument_id, payload)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        [
                            [publication_id, model_name, row["stable_key"], rank, row.get("instrument_id"), Jsonb(row["payload"])]
                            for rank, row in enumerate(rows, start=1)
                        ],
                    )
            connection.execute(
                "UPDATE app.publication SET status = 'superseded' "
                "WHERE scope = %s AND status = 'published'",
                [scope],
            )
            connection.execute(
                "UPDATE app.publication SET status = 'published', published_at = now() WHERE id = %s",
                [publication_id],
            )
            if complete_run_summary is not None:
                result = connection.execute(
                    "UPDATE analysis.run SET status = 'succeeded', finished_at = now(), "
                    "summary = summary || %s WHERE id = %s AND status = 'running'",
                    [Jsonb(dict(complete_run_summary)), run_id],
                )
                if result.rowcount != 1:
                    raise ValueError("atomic publication requires a running analysis run")
        return publication_id

    def publication_rows(self, scope: str, model_name: str) -> list[dict[str, Any]]:
        with self.runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT item.payload
                FROM app.publication publication
                JOIN app.publication_item item ON item.publication_id = publication.id
                WHERE publication.scope = %s AND publication.status = 'published'
                  AND item.model_name = %s
                ORDER BY item.rank
                """,
                [scope, model_name],
            ).fetchall()
        return [dict(row["payload"]) for row in rows]

    def option_signal_detail(self, decision_id: UUID) -> dict[str, Any] | None:
        """Return immutable signal, publication, evidence, and outcome context."""

        with self.runtime.read() as connection:
            row = connection.execute(
                """
                SELECT decision.id::text AS decision_id, decision.state, decision.rank,
                       decision.score AS rank_score, decision.as_of, decision.reasons,
                       decision.blockers, decision.quality_status,
                       instrument.symbol AS ticker, contract.expiration, contract.strike,
                       contract.option_type, contract.multiplier,
                       option_decision.*, strategy.strategy_key,
                       strategy.revision AS strategy_revision,
                       run.input_cutoff AS analysis_cutoff, run.code_version,
                       run.feature_versions,
                       publication.id::text AS publication_id,
                       publication.published_at,
                       outcome.maturity_state, outcome.observed_through,
                       outcome.current_return, outcome.return_1d, outcome.return_5d,
                       outcome.return_20d, outcome.return_60d, outcome.peak_return,
                       outcome.max_drawdown, outcome.paper_status,
                       outcome.credit_captured, outcome.collateral_return,
                       outcome.assigned_basis, outcome.strike_touched
                FROM analysis.decision decision
                JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
                JOIN analysis.run run ON run.id = decision.run_id
                LEFT JOIN analysis.strategy_revision strategy ON strategy.id = decision.strategy_revision_id
                LEFT JOIN analysis.option_outcome outcome ON outcome.decision_id = decision.id
                LEFT JOIN app.publication publication
                  ON publication.analysis_run_id = decision.run_id
                 AND publication.scope = 'options-radar'
                 AND publication.status IN ('published', 'superseded')
                WHERE decision.id = %s
                ORDER BY publication.published_at DESC NULLS LAST LIMIT 1
                """,
                [decision_id],
            ).fetchone()
            if row is None:
                return None
            evidence = connection.execute(
                "SELECT evidence_kind, reference_key, reference_url, detail "
                "FROM analysis.decision_evidence WHERE decision_id = %s "
                "ORDER BY evidence_kind, reference_key",
                [decision_id],
            ).fetchall()
            alternatives = connection.execute(
                """
                SELECT candidate.id::text AS decision_id, candidate.state,
                       candidate.score AS rank_score, candidate_option.structure,
                       candidate_option.entry_price, candidate_option.expected_value,
                       candidate_option.risk_adjusted_expectancy,
                       candidate_option.max_loss, candidate_option.secured_cash
                FROM analysis.decision chosen
                JOIN analysis.decision candidate
                  ON candidate.run_id = chosen.run_id
                 AND candidate.instrument_id = chosen.instrument_id
                 AND candidate.id <> chosen.id
                JOIN analysis.option_decision candidate_option ON candidate_option.decision_id = candidate.id
                WHERE chosen.id = %s
                ORDER BY candidate.score DESC NULLS LAST LIMIT 3
                """,
                [decision_id],
            ).fetchall()
        result = _jsonable(dict(row))
        result["contract_version"] = 2
        result["evidence"] = [_jsonable(dict(item)) for item in evidence]
        result["alternatives"] = [_jsonable(dict(item)) for item in alternatives]
        result["no_trade_baseline"] = {"structure": "no_trade", "expected_value": 0.0, "max_loss": 0.0}
        return result


def _prepare_models(models: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    prepared: dict[str, list[dict[str, Any]]] = {}
    for model_name, source_rows in models.items():
        if not model_name.strip():
            raise ValueError("publication model name is required")
        rows: list[dict[str, Any]] = []
        keys: set[str] = set()
        for source in source_rows:
            payload = _jsonable(dict(source))
            stable_key = str(
                payload.get("stable_key") or payload.get("decision_id") or payload.get("opportunity_id")
                or payload.get("event_id") or payload.get("contract_id") or payload.get("symbol") or _hash(payload)
            )
            if stable_key in keys:
                raise ValueError(f"duplicate publication key for {model_name}: {stable_key}")
            keys.add(stable_key)
            rows.append({"stable_key": stable_key, "instrument_id": payload.pop("instrument_id", None), "payload": payload})
        prepared[model_name] = rows
    return prepared


def _hash(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(canonical).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    return value
