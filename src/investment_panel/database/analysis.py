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
from investment_panel.core.decision import StrategyForecast, strategy_forecast_id_for_payload


CURRENT_OPTION_PUBLICATION_MODELS = frozenset({
    ("options-radar", "option_radar_opportunity"),
    ("options-decision-system", "options_decision_candidate"),
})
CURRENT_OPTION_MODEL_NAMES = frozenset(model for _, model in CURRENT_OPTION_PUBLICATION_MODELS)


def current_option_publication_answers(
    connection: Any,
    *,
    cutoff: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read current option answers once across both option publication owners."""

    return list(current_option_publication_answers_result(connection, cutoff=cutoff)["rows"])


def current_option_publication_answers_result(
    connection: Any,
    *,
    cutoff: datetime | None = None,
) -> dict[str, Any]:
    """Return current option answers and the cross-owner authority state."""

    rows: list[dict[str, Any]] = []
    owner_diagnostics: list[dict[str, Any]] = []
    for scope, model_name in sorted(CURRENT_OPTION_PUBLICATION_MODELS):
        result = current_option_publication_result(
            connection,
            scope=scope,
            model_name=model_name,
            cutoff=cutoff,
        )
        diagnostic = dict(result["authority"])
        if diagnostic.get("status") != "available":
            owner_diagnostics.append(diagnostic)
        rows.extend(
            {
                **row,
                "scope": scope,
            }
            for row in result["rows"]
        )
    blocking_diagnostics = [
        diagnostic for diagnostic in owner_diagnostics
        if diagnostic.get("reason") != "current_option_publication_empty"
    ]
    if blocking_diagnostics:
        return {
            "rows": [],
            "authority": {
                "status": "unavailable",
                "reason": "current_option_publication_authority_conflict",
                "owners": blocking_diagnostics,
            },
        }
    counts: dict[str, int] = {}
    for row in rows:
        episode_key = str(row.get("episode_key") or "")
        if episode_key:
            counts[episode_key] = counts.get(episode_key, 0) + 1
    answers = [
        row for row in rows
        if counts.get(str(row.get("episode_key") or ""), 0) == 1
    ]
    if len(answers) != len(rows):
        return {
            "rows": [],
            "authority": {
                "status": "unavailable",
                "reason": "current_option_duplicate_episode_authority",
                "source_row_count": len(rows),
                "returned_row_count": len(answers),
            },
        }
    if not rows:
        return {
            "rows": [],
            "authority": {
                "status": "unavailable",
                "reason": "current_option_publication_empty",
                "source_row_count": 0,
                "returned_row_count": 0,
            },
        }
    return {
        "rows": answers,
        "authority": {
            "status": "available",
            "reason": None,
            "source_row_count": len(rows),
            "returned_row_count": len(answers),
        },
    }


def current_option_publication_result(
    connection: Any,
    *,
    scope: str,
    model_name: str,
    cutoff: datetime | None = None,
    publication_id: UUID | str | None = None,
) -> dict[str, Any]:
    """Read one point-in-time option publication with authoritative episode keys.

    Publication payloads are not identity authority.  The option decision row
    and its PostgreSQL ``episode_key`` are the authority.  Any row without a
    resolvable decision, with conflicting aliases, or with duplicate episode
    authority invalidates the complete model projection so a malformed
    publication cannot become a current answer by selection order.
    """

    if (scope, model_name) not in CURRENT_OPTION_PUBLICATION_MODELS:
        raise ValueError("unsupported current option publication model")
    status_clause = (
        "publication.status = 'published'"
        if cutoff is None
        else "publication.status IN ('published', 'superseded')"
    )
    rows = connection.execute(
        f"""
        WITH chosen_publication AS MATERIALIZED (
            SELECT publication.id, publication.bundle_id, publication.published_at,
                   publication.analysis_run_id
            FROM app.publication publication
            JOIN analysis.run run ON run.id = publication.analysis_run_id
            WHERE publication.scope = %s
              AND {status_clause}
              AND publication.published_at IS NOT NULL
              AND publication.published_at <= COALESCE(%s::timestamptz, now())
              AND (%s::uuid IS NULL OR publication.id = %s::uuid)
            ORDER BY publication.published_at DESC, publication.created_at DESC,
                     publication.id DESC
            LIMIT 1
        ), source_rows AS MATERIALIZED (
            SELECT item.model_name, item.stable_key, item.rank,
                   chosen.id::text AS publication_id, chosen.published_at,
                   chosen.analysis_run_id, payload.payload
            FROM chosen_publication chosen
            JOIN app.publication_bundle_item item ON item.bundle_id = chosen.bundle_id
            JOIN app.publication_payload payload ON payload.content_hash = item.content_hash
            WHERE chosen.bundle_id IS NOT NULL AND item.model_name = %s
            UNION ALL
            SELECT item.model_name, item.stable_key, item.rank,
                   chosen.id::text AS publication_id, chosen.published_at,
                   chosen.analysis_run_id, item.payload
            FROM chosen_publication chosen
            JOIN app.publication_item item ON item.publication_id = chosen.id
            WHERE chosen.bundle_id IS NULL AND item.model_name = %s
        ), identified_rows AS MATERIALIZED (
            SELECT source.*,
                   NULLIF(BTRIM(source.payload->>'decision_id'), '') AS decision_identity,
                   NULLIF(BTRIM(source.payload->>'opportunity_id'), '') AS opportunity_identity,
                   NULLIF(BTRIM(source.payload->>'episode_key'), '') AS payload_episode_key,
                   NULLIF(BTRIM(source.payload->>'ticker'), '') AS payload_ticker,
                   NULLIF(BTRIM(source.payload->>'symbol'), '') AS payload_symbol
            FROM source_rows source
        ), resolved_rows AS MATERIALIZED (
            SELECT identified.*, decision.id AS authoritative_decision_id,
                   decision.kind AS authoritative_kind,
                   decision.run_id AS authoritative_run_id,
                   decision.episode_key AS authoritative_episode_key,
                   instrument.symbol AS authoritative_symbol
            FROM identified_rows identified
            LEFT JOIN analysis.decision decision
              ON decision.id::text = COALESCE(
                   identified.decision_identity, identified.opportunity_identity
                 )
            LEFT JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
        ), validated_rows AS MATERIALIZED (
            SELECT resolved.*,
                   (
                       resolved.authoritative_decision_id IS NOT NULL
                       AND resolved.authoritative_kind = 'option'
                       AND resolved.authoritative_run_id = resolved.analysis_run_id
                       AND NULLIF(BTRIM(resolved.authoritative_episode_key), '') IS NOT NULL
                   ) AS authority_valid,
                   (
                       resolved.authoritative_decision_id IS NOT NULL
                       AND resolved.authoritative_kind = 'option'
                       AND resolved.authoritative_run_id = resolved.analysis_run_id
                       AND NULLIF(BTRIM(resolved.authoritative_episode_key), '') IS NOT NULL
                       AND (
                           resolved.decision_identity IS NOT NULL
                           OR resolved.opportunity_identity IS NOT NULL
                       )
                       AND (
                           resolved.decision_identity IS NULL
                           OR resolved.opportunity_identity IS NULL
                           OR resolved.decision_identity = resolved.opportunity_identity
                       )
                       AND (
                           resolved.payload_episode_key IS NULL
                           OR resolved.payload_episode_key = resolved.authoritative_episode_key
                       )
                       AND (
                           resolved.payload_ticker IS NULL
                           OR upper(resolved.payload_ticker) = upper(resolved.authoritative_symbol)
                       )
                       AND (
                           resolved.payload_symbol IS NULL
                           OR upper(resolved.payload_symbol) = upper(resolved.authoritative_symbol)
                       )
                       AND (
                           resolved.payload_ticker IS NULL
                           OR resolved.payload_symbol IS NULL
                           OR upper(resolved.payload_ticker) = upper(resolved.payload_symbol)
                       )
                   ) AS row_valid
            FROM resolved_rows resolved
        ), counted_rows AS MATERIALIZED (
            SELECT validated.*,
                   count(*) OVER () AS source_row_count,
                   count(*) FILTER (WHERE validated.authority_valid) OVER () AS authoritative_row_count,
                   count(*) FILTER (WHERE validated.row_valid) OVER () AS valid_row_count,
                   count(*) FILTER (WHERE validated.authority_valid) OVER (
                       PARTITION BY validated.authoritative_episode_key
                   ) AS episode_authority_count
            FROM validated_rows validated
        )
        SELECT payload, publication_id, published_at, rank, stable_key,
               authoritative_decision_id::text AS authoritative_decision_id,
               authoritative_episode_key AS episode_key,
               source_row_count, authoritative_row_count, valid_row_count,
               episode_authority_count, row_valid
        FROM counted_rows
        ORDER BY rank, stable_key
        """,
        [scope, cutoff, publication_id, publication_id, model_name, model_name],
    ).fetchall()
    normalized = [dict(row) for row in rows]
    if not normalized:
        return {
            "rows": [],
            "authority": {
                "status": "unavailable",
                "scope": scope,
                "model_name": model_name,
                "reason": "current_option_publication_empty",
                "source_row_count": 0,
                "authoritative_row_count": 0,
                "valid_row_count": 0,
                "returned_row_count": 0,
                "duplicate_episode_count": 0,
            },
        }
    first = normalized[0]
    source_row_count = int(first["source_row_count"])
    authoritative_row_count = int(first["authoritative_row_count"])
    valid_row_count = int(first["valid_row_count"])
    accepted = [
        row for row in normalized
        if bool(row["row_valid"]) and int(row["episode_authority_count"]) == 1
    ]
    duplicate_episodes = {
        str(row["episode_key"])
        for row in normalized
        if bool(row["authoritative_decision_id"])
        and int(row["episode_authority_count"]) > 1
        and row.get("episode_key")
    }
    returned_row_count = len(accepted)
    complete = (
        source_row_count == authoritative_row_count == valid_row_count == returned_row_count
        and not duplicate_episodes
    )
    if complete:
        reason = None
        status = "available"
    elif duplicate_episodes:
        reason = "current_option_duplicate_episode_authority"
        status = "unavailable"
    elif authoritative_row_count != source_row_count:
        reason = "current_option_authority_unresolved"
        status = "unavailable"
    elif valid_row_count != source_row_count:
        reason = "current_option_identity_conflict"
        status = "unavailable"
    else:
        reason = "current_option_projection_incomplete"
        status = "unavailable"
    authority = {
        "status": status,
        "scope": scope,
        "model_name": model_name,
        "reason": reason,
        "source_row_count": source_row_count,
        "authoritative_row_count": authoritative_row_count,
        "valid_row_count": valid_row_count,
        "returned_row_count": returned_row_count,
        "duplicate_episode_count": len(duplicate_episodes),
    }
    if not complete:
        return {"rows": [], "authority": authority}
    for row in accepted:
        row.pop("source_row_count", None)
        row.pop("authoritative_row_count", None)
        row.pop("valid_row_count", None)
        row.pop("episode_authority_count", None)
        row.pop("row_valid", None)
    return {"rows": accepted, "authority": authority}


def current_option_publication_rows(
    connection: Any,
    *,
    scope: str,
    model_name: str,
    cutoff: datetime | None = None,
    publication_id: UUID | str | None = None,
) -> list[dict[str, Any]]:
    """Return only a complete current option projection."""

    return list(current_option_publication_result(
        connection,
        scope=scope,
        model_name=model_name,
        cutoff=cutoff,
        publication_id=publication_id,
    )["rows"])


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
        hypothesis_id: UUID | None = None,
        experiment_family_id: UUID | None = None,
        artifact_id: str | None = None,
        artifact_hash: str | None = None,
        research_required: bool = False,
    ) -> int:
        with self.runtime.transaction() as connection:
            row = connection.execute(
                f"""
                INSERT INTO analysis.strategy_revision
                    (strategy_key, revision, name, status, parameters, supersedes_id, authority_group,
                     hypothesis_id, experiment_family_id, artifact_id, artifact_hash,
                     research_required, promoted_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s = 'active' THEN now() ELSE NULL END)
                ON CONFLICT (strategy_key, revision) DO UPDATE
                SET name = EXCLUDED.name, parameters = EXCLUDED.parameters
                RETURNING id
                """,
                [
                    strategy_key, revision, name, status, Jsonb(dict(parameters)), supersedes_id,
                    authority_group or strategy_key, hypothesis_id, experiment_family_id,
                    artifact_id, artifact_hash, research_required, status,
                ],
            ).fetchone()
        return int(row["id"])

    def qualified_stock_alpha_artifact(
        self,
        *,
        cutoff: datetime,
        horizon: str,
    ) -> dict[str, Any]:
        """Return the active stock-alpha artifact and its bounded OOS proof."""

        with self.runtime.read() as connection:
            row = connection.execute(
                """
                SELECT strategy.id AS strategy_revision_id, strategy.strategy_key,
                       strategy.revision, strategy.parameters, strategy.created_at,
                       strategy.promoted_at,
                       evaluation.id::text AS strategy_evaluation_id,
                       evaluation.evaluation_type, evaluation.evaluated_at,
                       evaluation.available_at AS evaluation_available_at,
                       evaluation.period_start, evaluation.period_end,
                       evaluation.verdict, evaluation.metrics, evaluation.evidence,
                       promotion.id::text AS promotion_evaluation_id,
                       promotion.evaluated_at AS promotion_evaluated_at,
                       promotion.available_at AS promotion_available_at,
                       promotion.metrics AS promotion_metrics
                FROM analysis.strategy_revision strategy
                LEFT JOIN LATERAL (
                    SELECT candidate.*
                    FROM analysis.strategy_evaluation candidate
                    WHERE candidate.strategy_revision_id = strategy.id
                      AND candidate.evaluation_type IN ('out_of_sample', 'oos')
                      AND candidate.evaluated_at <= %s
                      AND candidate.available_at <= %s
                      AND (candidate.period_end IS NULL OR candidate.period_end <= %s)
                    ORDER BY candidate.evaluated_at DESC, candidate.id DESC
                    LIMIT 1
                ) evaluation ON TRUE
                LEFT JOIN LATERAL (
                    SELECT candidate.*
                    FROM analysis.strategy_evaluation candidate
                    WHERE candidate.strategy_revision_id = strategy.id
                      AND candidate.evaluation_type = 'paper_advisory_promotion'
                      AND candidate.verdict = 'pass'
                      AND candidate.evaluated_at <= %s
                      AND candidate.available_at <= %s
                    ORDER BY candidate.evaluated_at DESC, candidate.id DESC
                    LIMIT 1
                ) promotion ON TRUE
                WHERE strategy.strategy_key = 'ticker-stock-alpha'
                  AND strategy.status = 'active'
                  AND COALESCE(strategy.promoted_at, strategy.created_at) <= %s
                ORDER BY COALESCE(strategy.promoted_at, strategy.created_at) DESC,
                         strategy.revision DESC
                LIMIT 1
                """,
                [cutoff, cutoff, cutoff, cutoff, cutoff, cutoff],
            ).fetchone()
        if row is None:
            return {
                "availability_status": "missing",
                "blockers": ["alpha_strategy_revision_missing"],
            }
        parameters = dict(row["parameters"] or {})
        metrics = dict(row["metrics"] or {})
        base = {
            "strategy_key": str(row["strategy_key"]),
            "strategy_revision_id": int(row["strategy_revision_id"]),
            "strategy_revision": int(row["revision"]),
            "artifact_published_at": row["promoted_at"] or row["created_at"],
            "model_artifact_id": parameters.get("artifact_id"),
            "artifact_hash": parameters.get("artifact_hash"),
            "input_hash": parameters.get("input_hash"),
            "model_version": parameters.get("model_version"),
            "feature_version": parameters.get("feature_version"),
            "target": parameters.get("target"),
            "horizon": str(horizon),
            "cohort_id": parameters.get("cohort_id"),
            "calibration_state": parameters.get("calibration_state"),
            "strategy_evaluation_id": row["strategy_evaluation_id"],
            "evaluation_stage": row["evaluation_type"],
            "evaluation_evaluated_at": row["evaluated_at"],
            "evaluation_available_at": row["evaluation_available_at"],
            "oos_period_start": row["period_start"],
            "oos_period_end": row["period_end"],
            "cohort_path": metrics.get("cohort_path") or [],
            "fallback_parent": metrics.get("fallback_parent"),
            "effective_sample_size": metrics.get("effective_sample_size"),
            "calibration_metrics": metrics.get("calibration_metrics") or {},
            "cost_model_version": metrics.get("cost_model_version") or parameters.get("cost_model_version"),
            "lower_confidence_net_utility_after_costs": metrics.get(
                "lower_confidence_net_utility_after_costs"
            ),
            "promotion_stage": (
                str((row["promotion_metrics"] or {}).get("authorization_mode") or "").lower()
                if row["promotion_evaluation_id"] is not None else "challenger"
            ),
            "promotion_evaluation_id": row["promotion_evaluation_id"],
            "promotion_evaluated_at": row["promotion_evaluated_at"],
            "promotion_available_at": row["promotion_available_at"],
            "forecast": metrics.get("forecast"),
            "forecasts": metrics.get("forecasts"),
        }
        if row["strategy_evaluation_id"] is None:
            return {**base, "availability_status": "not_calibrated", "blockers": ["alpha_oos_evaluation_missing"]}
        if str(row["verdict"] or "").lower() != "pass":
            return {**base, "availability_status": "policy_blocked", "blockers": ["alpha_oos_evaluation_not_passed"]}
        if row["promotion_evaluation_id"] is None:
            return {**base, "availability_status": "policy_blocked", "blockers": ["alpha_promotion_evidence_missing"]}
        required = ("artifact_id", "model_version", "feature_version", "target", "cohort_id", "calibration_state")
        if any(not str(parameters.get(name) or "").strip() for name in required):
            return {**base, "availability_status": "error", "blockers": ["alpha_artifact_metadata_incomplete"]}
        configured_horizons = parameters.get("horizons", [parameters.get("horizon")])
        if isinstance(configured_horizons, str):
            configured_horizons = [configured_horizons]
        allowed_horizons = {
            str(item).upper()
            for item in configured_horizons
            if item is not None
        }
        if str(parameters.get("expression_kind") or "").upper() != "STOCK" or str(horizon).upper() not in allowed_horizons:
            return {**base, "availability_status": "policy_blocked", "blockers": ["alpha_artifact_scope_mismatch"]}
        lineage_names = (
            "artifact_id", "artifact_hash", "input_hash", "model_version",
            "feature_version", "cohort_id", "cost_model_version",
        )
        if any(
            str(metrics.get(name) or "") != str(parameters.get(name) or "")
            for name in lineage_names
        ):
            return {**base, "availability_status": "error", "blockers": ["alpha_evaluation_lineage_mismatch"]}
        promotion_metrics = dict(row["promotion_metrics"] or {})
        if (
            str(promotion_metrics.get("artifact_hash") or "") != str(metrics.get("artifact_hash") or "")
            or str(promotion_metrics.get("input_hash") or "") != str(metrics.get("input_hash") or "")
            or str(promotion_metrics.get("authorization_mode") or "").upper() not in {"PAPER", "ADVISORY"}
        ):
            return {**base, "availability_status": "error", "blockers": ["alpha_promotion_lineage_mismatch"]}
        phase2_values = {
            "oos_period_start": row["period_start"],
            "oos_period_end": row["period_end"],
            "cohort_path": metrics.get("cohort_path"),
            "effective_sample_size": metrics.get("effective_sample_size"),
            "calibration_metrics": metrics.get("calibration_metrics"),
            "cost_model_version": metrics.get("cost_model_version"),
            "lower_confidence_net_utility_after_costs": metrics.get("lower_confidence_net_utility_after_costs"),
        }
        if any(value is None or value == [] or value == {} for value in phase2_values.values()):
            return {**base, "availability_status": "error", "blockers": ["alpha_phase2_evidence_incomplete"]}
        try:
            if int(metrics["effective_sample_size"]) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return {**base, "availability_status": "error", "blockers": ["alpha_effective_sample_invalid"]}
        valid_through = metrics.get("valid_through")
        if valid_through is not None:
            try:
                valid_until = datetime.fromisoformat(str(valid_through).replace("Z", "+00:00"))
            except ValueError:
                return {**base, "availability_status": "error", "blockers": ["alpha_evaluation_validity_invalid"]}
            if valid_until < cutoff:
                return {**base, "availability_status": "stale", "blockers": ["alpha_evaluation_stale"]}
        return {**base, "availability_status": "available", "blockers": []}

    def stock_alpha_feature(
        self,
        symbol: str,
        *,
        cutoff: datetime,
        feature_version: str,
    ) -> dict[str, Any] | None:
        """Return the latest cutoff-valid canonical trend feature for live alpha."""

        with self.runtime.read() as connection:
            row = connection.execute(
                """
                SELECT instrument.symbol, feature.as_of,
                       analysis_run.input_cutoff AS available_at,
                       'analysis.symbol_feature' AS source,
                       feature.feature_version AS source_version,
                       feature.id::text AS revision,
                       feature.feature_version, feature.momentum_5d,
                       feature.momentum_20d, feature.relative_strength_20d,
                       feature.relative_strength_60d, feature.kaufman_er_20d
                FROM analysis.symbol_feature feature
                JOIN analysis.run analysis_run ON analysis_run.id = feature.run_id
                JOIN catalog.instrument instrument ON instrument.id = feature.instrument_id
                WHERE instrument.symbol = %s
                  AND feature.feature_set = 'daily_trend'
                  AND feature.feature_version = %s
                  AND feature.as_of <= %s
                  AND analysis_run.input_cutoff <= %s
                  AND feature.data_quality_status = 'complete'
                ORDER BY feature.as_of DESC, analysis_run.input_cutoff DESC, feature.id DESC
                LIMIT 1
                """,
                [symbol.strip().upper(), feature_version, cutoff, cutoff],
            ).fetchone()
        return dict(row) if row is not None else None

    def store_strategy_forecast(self, forecast: Mapping[str, Any]) -> str | None:
        """Persist one content-addressed model forecast exactly once.

        This boundary accepts only a complete ``StrategyForecast`` payload.
        Caller IDs, zero hashes, partial legacy signal dictionaries, and
        conflicting duplicate identities are rejected.
        """
        try:
            model = forecast if isinstance(forecast, StrategyForecast) else StrategyForecast.model_validate(forecast)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid model-owned strategy forecast: {exc}") from exc
        payload = model.model_dump(mode="json")
        forecast_id = model.strategy_forecast_id
        if forecast_id != strategy_forecast_id_for_payload(payload):
            raise ValueError("strategy forecast identity does not match its immutable payload")
        ticker = model.ticker.strip().upper()
        with self.runtime.transaction(JOB_PROFILE) as connection:
            instrument = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = %s LIMIT 1", [ticker]
            ).fetchone()
            if instrument is None:
                raise ValueError(f"forecast instrument is not persisted: {ticker}")
            existing = connection.execute(
                """
                SELECT forecast.*, instrument.symbol AS ticker
                FROM analysis.strategy_forecast forecast
                JOIN catalog.instrument instrument ON instrument.id = forecast.instrument_id
                WHERE forecast.id = %s
                FOR KEY SHARE OF forecast
                """,
                [forecast_id],
            ).fetchone()
            if existing is not None:
                existing_values = {
                    "strategy_revision_id": existing["strategy_revision_id"],
                    "strategy_evaluation_id": str(existing["strategy_evaluation_id"]) if existing["strategy_evaluation_id"] is not None else None,
                    "ticker": existing["ticker"],
                    "opportunity_episode_id": existing["opportunity_episode_id"],
                    "target": existing["target"],
                    "horizon": existing["horizon"],
                    "forecast_value": existing["forecast_value"],
                    "forecast_range": existing["forecast_range"],
                    "forecast_distribution": existing["forecast_distribution"],
                    "probability_semantics": existing["probability_semantics"],
                    "model_artifact_id": existing["model_artifact_id"],
                    "artifact_hash": existing["artifact_hash"],
                    "input_hash": existing["input_hash"],
                    "as_of": existing["as_of"],
                    "input_cutoff": existing["input_cutoff"],
                    "generated_at": existing["generated_at"],
                    "available_at": existing["available_at"],
                }
                incoming_values = {
                    key: payload[key]
                    for key in existing_values
                    if key in payload
                }
                for key in ("as_of", "input_cutoff", "generated_at", "available_at"):
                    incoming_values[key] = model.model_dump()[key]
                if any(
                    str(existing_values[key]) != str(incoming_values[key])
                    if key in {"strategy_evaluation_id", "as_of", "input_cutoff", "generated_at", "available_at"}
                    else existing_values[key] != incoming_values[key]
                    for key in existing_values
                ):
                    raise ValueError("strategy forecast identity conflicts with persisted immutable payload")
                return str(existing["id"])
            connection.execute(
                """
                INSERT INTO analysis.strategy_forecast (
                    id, strategy_revision_id, strategy_evaluation_id, instrument_id,
                    opportunity_episode_id, target, horizon, forecast_value,
                    forecast_range, forecast_distribution, probability_semantics,
                    model_artifact_id, artifact_hash, input_hash, as_of, input_cutoff,
                    generated_at, available_at, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                [
                    forecast_id, model.strategy_revision_id, model.strategy_evaluation_id,
                    instrument["id"], model.opportunity_episode_id, model.target, model.horizon,
                    model.forecast_value, Jsonb(model.forecast_range.model_dump(mode="json")) if model.forecast_range else None,
                    Jsonb(model.forecast_distribution) if model.forecast_distribution else None,
                    model.probability_semantics, model.model_artifact_id, model.artifact_hash,
                    model.input_hash, model.as_of, model.input_cutoff, model.generated_at,
                    model.available_at, Jsonb({"contract_version": model.contract_version}),
                ],
            )
        return forecast_id

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
            bundle_rows = _bundle_rows(prepared)
            bundle_hash = _hash({"scope": scope, "items": bundle_rows})
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
            if (scope, model_name) in CURRENT_OPTION_PUBLICATION_MODELS:
                rows = current_option_publication_rows(
                    connection, scope=scope, model_name=model_name,
                )
            else:
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
                SELECT publication.id, publication.bundle_id, publication.published_at
                FROM app.publication publication
                JOIN analysis.run run ON run.id = publication.analysis_run_id
                WHERE publication.scope = %s
                  AND publication.status IN ('published', 'superseded')
                  AND run.input_cutoff < %s
                  AND publication.published_at IS NOT NULL
                  AND publication.published_at <= %s
                  AND (%s::text IS NULL OR run.inputs->>'source_id' = %s)
                ORDER BY run.input_cutoff DESC, publication.published_at DESC NULLS LAST,
                         publication.id DESC LIMIT 1
                """,
                [scope, cutoff, cutoff, source_id, source_id],
            ).fetchone()
            if predecessor is None:
                return []
            if (scope, model_name) in CURRENT_OPTION_PUBLICATION_MODELS:
                rows = current_option_publication_rows(
                    connection,
                    scope=scope,
                    model_name=model_name,
                    cutoff=cutoff,
                    publication_id=predecessor["id"],
                )
                return [dict(row["payload"] or {}) for row in rows]
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
        if (scope, model_name) in CURRENT_OPTION_PUBLICATION_MODELS:
            with self.runtime.read() as connection:
                rows = current_option_publication_rows(
                    connection,
                    scope=scope,
                    model_name=model_name,
                    cutoff=cutoff,
                    publication_id=publication["publication_id"],
                )
            return [dict(row["payload"] or {}) for row in rows]
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
            current_answers = current_option_publication_answers_result(connection)
            current_matches = [
                candidate
                for candidate in current_answers["rows"]
                if str(
                    (candidate["payload"] or {}).get("decision_id")
                    or (candidate["payload"] or {}).get("opportunity_id")
                    or ""
                ) == str(decision_id)
            ]
            current_item = current_matches[0] if len(current_matches) == 1 else None
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
            authority = dict(current_answers["authority"])
            blocker = (
                "current_option_publication_authority_unavailable"
                if authority.get("status") != "available"
                and authority.get("reason") != "current_option_publication_empty"
                else "not_in_current_publication"
            )
            result["blockers"] = sorted(set([*list(result.get("blockers") or []), blocker]))
            if authority.get("status") != "available":
                result["authority"] = authority
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
            episode_key = str(payload.get("episode_key") or "").strip()
            stable_key = str(
                f"episode:{episode_key}"
                if model_name in CURRENT_OPTION_MODEL_NAMES
                and episode_key
                else payload.get("stable_key") or payload.get("stable_unit_key")
                or payload.get("decision_id") or payload.get("opportunity_id")
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
