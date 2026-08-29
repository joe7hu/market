"""Reproducible analysis runs and atomic PostgreSQL publications."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from typing import Any, Mapping, Sequence
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.opportunity_episodes import (
    canonical_option_lane,
    option_episode_key,
    option_sample_eligibility,
    scorecard_truth_cohort,
)
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
                     strategy_revision_id, input_hash, inputs, started_at, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now(), 'running')
                RETURNING id
                """,
                [run_type, input_cutoff, code_version, Jsonb(dict(feature_versions or {})), strategy_revision_id, input_hash, Jsonb(_jsonable(dict(inputs)))],
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
        lane: str | None = None,
    ) -> UUID:
        option = dict(details or {})
        with self.runtime.transaction(JOB_PROFILE) as connection:
            instrument = connection.execute(
                "SELECT symbol FROM catalog.instrument WHERE id = %s", [instrument_id]
            ).fetchone()
            if instrument is None:
                raise ValueError(f"unknown option instrument: {instrument_id}")
            symbol = str(instrument["symbol"])
            nested_details = option.get("details")
            event_id = option.get("event_id")
            if not event_id and isinstance(nested_details, Mapping):
                event_id = nested_details.get("event_id")
            decision_lane = canonical_option_lane(lane, symbol=symbol)
            quality_status, sample_eligible, quarantine_reason = option_sample_eligibility(
                option.get("quality_status")
            )
            episode_key = option_episode_key(
                lane=decision_lane,
                symbol=symbol,
                strategy=str(option.get("strategy_key") or option.get("structure") or "option"),
                contract_ladder_slot=str(option.get("contract_ladder_slot") or contract_id),
                entry_at=quote_observed_at,
                event_id=str(event_id or "") or None,
            )
            calibration_cohort = scorecard_truth_cohort(
                str(option.get("calibration_cohort") or option.get("objective_version") or "default")
            )
            decision = connection.execute(
                """
                INSERT INTO analysis.decision
                    (run_id, decision_key, kind, instrument_id, as_of, state, rank, score,
                     quality_status, strategy_revision_id, reasons, blockers, input_hash,
                     lane, episode_key, sample_eligible, quarantine_reason, calibration_cohort)
                VALUES (%s, %s, 'option', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, decision_key) DO UPDATE
                SET state = EXCLUDED.state, rank = EXCLUDED.rank, score = EXCLUDED.score,
                    quality_status = EXCLUDED.quality_status, reasons = EXCLUDED.reasons,
                    blockers = EXCLUDED.blockers, input_hash = EXCLUDED.input_hash,
                    lane = EXCLUDED.lane, episode_key = EXCLUDED.episode_key,
                    sample_eligible = EXCLUDED.sample_eligible,
                    quarantine_reason = EXCLUDED.quarantine_reason,
                    calibration_cohort = EXCLUDED.calibration_cohort
                RETURNING id
                """,
                [
                    run_id, decision_key, instrument_id, quote_observed_at, state, rank, score,
                    quality_status, strategy_revision_id, list(reasons), list(blockers), _hash(inputs),
                    decision_lane, episode_key, sample_eligible, quarantine_reason,
                    calibration_cohort,
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
        bundle_rows = _bundle_rows(prepared)
        bundle_hash = _hash({"scope": scope, "items": bundle_rows})
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"publication:{scope}"])
            if strategy_root_key:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    [f"strategy:{strategy_root_key}"],
                )
            run = connection.execute(
                "SELECT status, strategy_revision_id, input_hash, code_version "
                "FROM analysis.run WHERE id = %s FOR UPDATE",
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
            existing = connection.execute(
                """
                SELECT publication.id, publication.status, publication.bundle_id
                FROM app.publication publication
                JOIN analysis.run prior_run ON prior_run.id = publication.analysis_run_id
                WHERE publication.scope = %s
                  AND publication.status IN ('published', 'superseded')
                  AND prior_run.input_hash = %s
                  AND prior_run.code_version = %s
                ORDER BY CASE publication.status WHEN 'published' THEN 0 ELSE 1 END,
                         publication.published_at DESC NULLS LAST,
                         publication.created_at DESC, publication.id DESC
                LIMIT 1
                FOR UPDATE OF publication
                """,
                [scope, run["input_hash"], run["code_version"]],
            ).fetchone()
            if existing is not None:
                existing_id = UUID(str(existing["id"]))
                # A repeated exact input after an intervening publication must
                # re-activate the prior immutable generation, not write its
                # rows again.  This closes the historical market-publication
                # explosion where the de-duplication query saw only the latest
                # published row and ignored an equivalent superseded one.
                if str(existing["status"]) != "published":
                    connection.execute(
                        "UPDATE app.publication SET status = 'superseded' "
                        "WHERE scope = %s AND status = 'published' AND id <> %s",
                        [scope, existing_id],
                    )
                    connection.execute(
                        "UPDATE app.publication SET status = 'published', published_at = now() "
                        "WHERE id = %s",
                        [existing_id],
                    )
                if existing["bundle_id"] is not None:
                    _replace_current_projection(
                        connection,
                        scope=scope,
                        publication_id=existing_id,
                        bundle_id=UUID(str(existing["bundle_id"])),
                    )
                else:
                    # A legacy generation has no compact bundle.  Clear any
                    # newer projection so readers use the legacy fallback.
                    connection.execute("DELETE FROM app.current_publication_item WHERE scope = %s", [scope])
                summary = dict(complete_run_summary or {})
                summary["reused_publication_id"] = str(existing_id)
                connection.execute(
                    """
                    UPDATE analysis.run
                    SET status = CASE WHEN status = 'running' THEN 'succeeded' ELSE status END,
                        finished_at = coalesce(finished_at, now()),
                        summary = summary || %s
                    WHERE id = %s
                    """,
                    [Jsonb(summary), run_id],
                )
                return existing_id
            bundle = connection.execute(
                """
                INSERT INTO app.publication_bundle (scope, bundle_hash, item_count)
                VALUES (%s, %s, %s)
                ON CONFLICT (scope, bundle_hash) DO NOTHING
                RETURNING id
                """,
                [scope, bundle_hash, len(bundle_rows)],
            ).fetchone()
            bundle_created = bundle is not None
            if bundle is None:
                bundle = connection.execute(
                    "SELECT id FROM app.publication_bundle WHERE scope = %s AND bundle_hash = %s FOR UPDATE",
                    [scope, bundle_hash],
                ).fetchone()
            if bundle is None:
                raise RuntimeError("publication bundle could not be resolved")
            bundle_id = UUID(str(bundle["id"]))
            if bundle_created:
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO app.publication_payload (content_hash, payload)
                        VALUES (%s, %s) ON CONFLICT (content_hash) DO NOTHING
                        """,
                        [[row["content_hash"], Jsonb(row["payload"])] for row in bundle_rows],
                    )
                    cursor.executemany(
                        """
                        INSERT INTO app.publication_bundle_item
                            (bundle_id, model_name, stable_key, rank, instrument_id, content_hash)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        [[bundle_id, row["model_name"], row["stable_key"], row["rank"],
                          row["instrument_id"], row["content_hash"]] for row in bundle_rows],
                    )
            publication = connection.execute(
                """
                INSERT INTO app.publication (scope, analysis_run_id, status, validation, bundle_id)
                VALUES (%s, %s, 'building', %s, %s) RETURNING id
                """,
                [scope, run_id, Jsonb(dict(validation or {})), bundle_id],
            ).fetchone()
            publication_id = UUID(str(publication["id"]))
            _replace_current_projection(
                connection,
                scope=scope,
                publication_id=publication_id,
                bundle_id=bundle_id,
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

    def publication_rows(
        self,
        scope: str,
        model_name: str,
        *,
        include_lineage: bool = False,
    ) -> list[dict[str, Any]]:
        with self.runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT payload.payload, publication.id::text AS publication_id,
                       publication.published_at
                FROM app.current_publication_item item
                JOIN app.publication_payload payload ON payload.content_hash = item.content_hash
                JOIN app.publication publication ON publication.id = item.publication_id
                WHERE item.scope = %s AND item.model_name = %s AND publication.status = 'published'
                ORDER BY item.rank
                """,
                [scope, model_name],
            ).fetchall()
            has_projection = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM app.current_publication_item item
                    JOIN app.publication publication ON publication.id = item.publication_id
                    WHERE item.scope = %s AND publication.status = 'published'
                ) AS exists
                """,
                [scope],
            ).fetchone()["exists"]
            if not rows and not has_projection:
                rows = connection.execute(
                    """
                    SELECT item.payload
                           , publication.id::text AS publication_id,
                           publication.published_at
                    FROM app.publication publication
                    JOIN app.publication_item item ON item.publication_id = publication.id
                    WHERE publication.scope = %s AND publication.status = 'published'
                      AND item.model_name = %s
                    ORDER BY item.rank
                    """,
                    [scope, model_name],
                ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row["payload"] or {})
            if include_lineage:
                if model_name in {"trade_plan", "outcome_attribution"} or "publication_id" not in payload:
                    payload["publication_id"] = str(row["publication_id"])
                if row["published_at"] is not None:
                    payload.setdefault("publication_published_at", row["published_at"].isoformat())
            output.append(payload)
        return output

    def publication_rows_before(
        self, scope: str, model_name: str, *, cutoff: datetime, source_id: str | None
    ) -> list[dict[str, Any]]:
        """Read the latest matching immutable publication strictly before a cutoff."""
        with self.runtime.read() as connection:
            predecessor = connection.execute(
                """
                SELECT publication.id, publication.bundle_id
                FROM app.publication publication
                JOIN analysis.run run ON run.id = publication.analysis_run_id
                WHERE publication.scope = %s
                  AND publication.status IN ('published', 'superseded')
                  AND run.input_cutoff < %s
                  AND (%s::text IS NULL OR run.inputs->>'source_id' = %s)
                ORDER BY run.input_cutoff DESC, publication.published_at DESC NULLS LAST,
                         publication.id DESC LIMIT 1
                """,
                [scope, cutoff, source_id, source_id],
            ).fetchone()
            if predecessor is None:
                return []
            if predecessor["bundle_id"] is not None:
                rows = connection.execute(
                    """
                    SELECT payload.payload
                    FROM app.publication_bundle_item item
                    JOIN app.publication_payload payload ON payload.content_hash = item.content_hash
                    WHERE item.bundle_id = %s AND item.model_name = %s
                    ORDER BY item.rank
                    """,
                    [predecessor["bundle_id"], model_name],
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT item.payload FROM app.publication_item item
                    WHERE item.publication_id = %s AND item.model_name = %s
                    ORDER BY item.rank
                    """,
                    [predecessor["id"], model_name],
                ).fetchall()
        return [dict(row["payload"]) for row in rows]

    def publication_at_or_before(
        self,
        scope: str,
        *,
        cutoff: datetime,
        source_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Select the latest immutable publication available at a cutoff."""

        if cutoff.tzinfo is None:
            raise ValueError("publication cutoff must be timezone-aware")
        with self.runtime.read() as connection:
            row = connection.execute(
                """
                SELECT publication.id::text AS publication_id, publication.bundle_id,
                       publication.published_at, publication.status, publication.scope,
                       run.input_cutoff, run.code_version, run.feature_versions,
                       run.input_hash, run.inputs, run.summary
                FROM app.publication publication
                JOIN analysis.run run ON run.id = publication.analysis_run_id
                WHERE publication.scope = %s
                  AND publication.status IN ('published', 'superseded')
                  AND run.input_cutoff <= %s
                  AND publication.published_at <= %s
                  AND (%s::text IS NULL OR run.inputs->>'source_id' = %s)
                ORDER BY run.input_cutoff DESC, publication.published_at DESC NULLS LAST,
                         publication.id DESC
                LIMIT 1
                """,
                [scope, cutoff, cutoff, source_id, source_id],
            ).fetchone()
            if row is None:
                return None
            payload_rows = _publication_payload_rows(connection, row)
        return _publication_result(row, payload_rows)

    def publication_by_id(
        self,
        scope: str,
        publication_id: str | UUID,
    ) -> dict[str, Any] | None:
        """Return one exact publication without applying a fact cutoff to visibility."""

        try:
            publication_uuid = UUID(str(publication_id))
        except (AttributeError, TypeError, ValueError):
            return None
        with self.runtime.read() as connection:
            row = connection.execute(
                """
                SELECT publication.id::text AS publication_id, publication.bundle_id,
                       publication.published_at, publication.status, publication.scope,
                       run.input_cutoff, run.code_version, run.feature_versions,
                       run.input_hash, run.inputs, run.summary
                FROM app.publication publication
                JOIN analysis.run run ON run.id = publication.analysis_run_id
                WHERE publication.scope = %s
                  AND publication.id = %s
                """,
                [scope, publication_uuid],
            ).fetchone()
            if row is None:
                return None
            payload_rows = _publication_payload_rows(connection, row)
        return _publication_result(row, payload_rows)

    def publication_rows_at_or_before(
        self,
        scope: str,
        model_name: str,
        *,
        cutoff: datetime,
        source_id: str | None = None,
    ) -> list[dict[str, Any]]:
        publication = self.publication_at_or_before(scope, cutoff=cutoff, source_id=source_id)
        if publication is None:
            return []
        return list(publication["models"].get(model_name) or [])

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
                       candidate_publication.payload->>'ranking_version' AS ranking_version,
                       NULLIF(candidate_publication.payload->>'research_rank', '')::integer AS research_rank,
                       NULLIF(candidate_publication.payload->>'trade_rank', '')::integer AS trade_rank,
                       NULLIF(candidate_publication.payload->>'execution_quality_score', '')::numeric AS execution_quality_score,
                       candidate_option.entry_price, candidate_option.expected_value,
                       candidate_option.risk_adjusted_expectancy,
                       candidate_option.max_loss, candidate_option.secured_cash
                FROM analysis.decision chosen
                JOIN analysis.decision candidate
                  ON candidate.run_id = chosen.run_id
                 AND candidate.instrument_id = chosen.instrument_id
                 AND candidate.id <> chosen.id
                JOIN analysis.option_decision candidate_option ON candidate_option.decision_id = candidate.id
                LEFT JOIN LATERAL (
                    SELECT item.payload
                    FROM app.publication publication
                    JOIN app.publication_content_item item ON item.publication_id = publication.id
                    WHERE publication.scope = 'options-radar'
                      AND publication.status = 'published'
                      AND item.model_name = 'option_radar_opportunity'
                      AND item.payload->>'decision_id' = candidate.id::text
                    ORDER BY publication.published_at DESC NULLS LAST, publication.created_at DESC
                    LIMIT 1
                ) candidate_publication ON true
                WHERE chosen.id = %s
                ORDER BY research_rank NULLS LAST, candidate.id LIMIT 3
                """,
                [decision_id],
            ).fetchall()
            current_item = connection.execute(
                """
                SELECT payload.payload, item.publication_id::text AS publication_id,
                       publication.published_at, item.scope
                FROM app.current_publication_item item
                JOIN app.publication_payload payload ON payload.content_hash = item.content_hash
                JOIN app.publication publication ON publication.id = item.publication_id
                WHERE publication.status = 'published'
                  AND (
                    (item.scope = 'options-radar' AND item.model_name = 'option_radar_opportunity')
                    OR
                    (item.scope = 'options-decision-system' AND item.model_name = 'options_decision_candidate')
                  )
                  AND payload.payload->>'decision_id' = %s
                ORDER BY publication.published_at DESC NULLS LAST
                LIMIT 1
                """,
                [str(decision_id)],
            ).fetchone()
            if current_item is None:
                current_item = connection.execute(
                    """
                SELECT item.payload, publication.id::text AS publication_id,
                       publication.published_at, publication.scope
                FROM app.publication publication
                JOIN app.publication_item item ON item.publication_id = publication.id
                WHERE publication.status = 'published'
                  AND (
                    (publication.scope = 'options-radar' AND item.model_name = 'option_radar_opportunity')
                    OR
                    (publication.scope = 'options-decision-system' AND item.model_name = 'options_decision_candidate')
                  )
                  AND item.payload->>'decision_id' = %s
                ORDER BY publication.published_at DESC NULLS LAST
                LIMIT 1
                """,
                [str(decision_id)],
            ).fetchone()
        result = _jsonable(dict(row))
        if current_item is not None:
            result.update(_jsonable(dict(current_item["payload"] or {})))
            result["publication_id"] = str(current_item["publication_id"])
            result["published_at"] = _jsonable(current_item["published_at"])
            result["publication_scope"] = str(current_item["scope"])
            result["current_publication"] = True
        else:
            result["current_publication"] = False
            result["execution_ready"] = False
            result["blockers"] = sorted(set([*list(result.get("blockers") or []), "not_in_current_publication"]))
        result["contract_version"] = 3
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
                payload.get("stable_key") or payload.get("stable_unit_key") or payload.get("decision_id") or payload.get("opportunity_id")
                or payload.get("event_id") or payload.get("contract_id") or payload.get("symbol") or _hash(payload)
            )
            if stable_key in keys:
                raise ValueError(f"duplicate publication key for {model_name}: {stable_key}")
            keys.add(stable_key)
            rows.append({"stable_key": stable_key, "instrument_id": payload.pop("instrument_id", None), "payload": payload})
        prepared[model_name] = rows
    return prepared


def _bundle_rows(prepared: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """Flatten a canonical publication bundle into deduplicated payload refs."""

    rows: list[dict[str, Any]] = []
    for model_name in sorted(prepared):
        for rank, row in enumerate(prepared[model_name], start=1):
            payload = dict(row["payload"])
            rows.append({
                "model_name": model_name,
                "stable_key": str(row["stable_key"]),
                "rank": rank,
                "instrument_id": row.get("instrument_id"),
                "content_hash": _hash(payload),
                "payload": payload,
            })
    return rows


def _replace_current_projection(
    connection: Any,
    *,
    scope: str,
    publication_id: UUID,
    bundle_id: UUID,
) -> None:
    """Replace one scope's small hot read model from an immutable bundle."""

    connection.execute("DELETE FROM app.current_publication_item WHERE scope = %s", [scope])
    connection.execute(
        """
        INSERT INTO app.current_publication_item
            (scope, publication_id, model_name, stable_key, rank, instrument_id, content_hash)
        SELECT %s, %s, model_name, stable_key, rank, instrument_id, content_hash
        FROM app.publication_bundle_item
        WHERE bundle_id = %s
        """,
        [scope, publication_id, bundle_id],
    )


def _publication_payload_rows(connection: Any, row: Mapping[str, Any]) -> Sequence[Any]:
    if row["bundle_id"] is not None:
        return connection.execute(
            """
            SELECT item.model_name, item.rank, payload.payload
            FROM app.publication_bundle_item item
            JOIN app.publication_payload payload ON payload.content_hash = item.content_hash
            WHERE item.bundle_id = %s
            ORDER BY item.model_name, item.rank
            """,
            [row["bundle_id"]],
        ).fetchall()
    return connection.execute(
        """
        SELECT item.model_name, item.rank, item.payload
        FROM app.publication_item item
        WHERE item.publication_id = %s
        ORDER BY item.model_name, item.rank
        """,
        [row["publication_id"]],
    ).fetchall()


def _publication_result(row: Mapping[str, Any], payload_rows: Sequence[Any]) -> dict[str, Any]:
    run_inputs = dict(row["inputs"] or {})
    source_lineage = run_inputs.get("source_lineage") or run_inputs.get("lineage") or run_inputs
    metadata = {
        "publication_id": str(row["publication_id"]),
        "publication_status": str(row["status"]),
        "publication_scope": str(row["scope"]),
        "input_cutoff": _iso(row["input_cutoff"]),
        "publication_input_cutoff": _iso(row["input_cutoff"]),
        "published_at": _iso(row["published_at"]),
        "publication_published_at": _iso(row["published_at"]),
        "code_version": row["code_version"],
        "feature_versions": dict(row["feature_versions"] or {}),
        "input_hash": row["input_hash"],
        "source_lineage": _jsonable(source_lineage),
        "summary": _jsonable(dict(row["summary"] or {})),
    }
    models: dict[str, list[dict[str, Any]]] = {}
    for payload_row in payload_rows:
        payload = dict(payload_row["payload"] or {})
        payload.update({key: value for key, value in metadata.items() if key not in payload})
        models.setdefault(str(payload_row["model_name"]), []).append(payload)
    return {**metadata, "models": models}


def _hash(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(canonical).hexdigest()


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, (datetime, date)) else str(value) if value is not None else None


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
