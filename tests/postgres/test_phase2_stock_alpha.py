from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from investment_panel.analysis.stock_alpha import FEATURE_VERSION, research_score
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.jobs.stock_alpha_walk_forward import load_observations, run


def _observations(count: int, cutoff: datetime) -> list[dict[str, object]]:
    start = cutoff - timedelta(days=count + 10)
    return [{
        "ticker": f"S{index:02d}",
        "horizon": "TACTICAL",
        "cohort_id": "large-liquid",
        "as_of": start + timedelta(days=index),
        "outcome_available_at": start + timedelta(days=index, hours=1),
        "feature_available_at": start + timedelta(days=index, minutes=-30),
        "outcome": 1.0,
        "realized_return": 0.05,
        "modeled_cost": 0.001,
        "features": {
            "feature_version": FEATURE_VERSION,
            "momentum_5d": 0.02,
            "momentum_20d": 0.04,
            "relative_strength_20d": 0.03,
            "relative_strength_60d": 0.06,
            "kaufman_er_20d": 0.5,
        },
    } for index in range(count)]


def _controls() -> dict[str, list[float]]:
    return {"randomized_label_returns": [0.0, 0.0], "white_noise_market_returns": [0.0, 0.0]}


def _seed_universe_tape(runtime: DatabaseRuntime, cutoff: datetime, symbols: list[str], *, as_of: datetime | None = None) -> None:
    tape_as_of = as_of or cutoff
    with runtime.transaction() as connection:
        connection.execute(
            """INSERT INTO analysis.ticker_benchmark_snapshot
               (benchmark_key, as_of, available_at, membership_hash, member_count, source_id, exact_membership)
               VALUES ('market-equity-etf', %s, %s, %s, %s, 'phase1-test', %s)""",
            [tape_as_of, tape_as_of - timedelta(seconds=1), "phase1-membership", len(symbols), Jsonb({symbol: True for symbol in symbols})],
        )


def test_walk_forward_registry_is_append_only_idempotent_and_paper_promoted(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(seconds=5)
        observations = _observations(16, cutoff)
        _seed_universe_tape(runtime, cutoff, [f"S{index:02d}" for index in range(16)], as_of=cutoff - timedelta(microseconds=2))
        first = run(
            runtime, observations, cutoff=cutoff, promote=True, authorization_mode="PAPER",
            min_train=4, fold_size=2, min_cohort=4, universe_members=[f"S{index:02d}" for index in range(16)], control_results=_controls(),
        )
        second = run(
            runtime, reversed(observations), cutoff=cutoff, promote=True, authorization_mode="PAPER",
            min_train=4, fold_size=2, min_cohort=4, universe_members=[f"S{index:02d}" for index in range(16)], control_results=_controls(),
        )

        assert first["complete"] is True
        assert first["promotion_stage"] == "paper"
        assert second["strategy_revision_id"] == first["strategy_revision_id"]
        assert second["strategy_evaluation_id"] == first["strategy_evaluation_id"]
        assert second["promotion_evaluation_id"] == first["promotion_evaluation_id"]
        with runtime.read() as connection:
            counts = connection.execute(
                """
                SELECT count(*) AS revisions,
                       (SELECT count(*) FROM analysis.strategy_evaluation
                        WHERE strategy_revision_id = %s) AS evaluations
                FROM analysis.strategy_revision
                WHERE strategy_key = 'ticker-stock-alpha'
                """,
                [first["strategy_revision_id"]],
            ).fetchone()
        assert counts == {"revisions": 1, "evaluations": 2}
        with runtime.read() as connection:
            research = connection.execute(
                """
                SELECT trial.status, result.outcome->'gates' AS gates,
                       result.metrics->'multiple_testing'->>'dsr' AS dsr,
                       (SELECT count(*) FROM analysis.validation_gate_result gate
                        JOIN analysis.validation_dossier dossier ON dossier.id = gate.dossier_id
                        WHERE dossier.strategy_revision_id = trial_revision.id) AS gate_count
                FROM analysis.research_trial trial
                JOIN analysis.validation_dossier dossier ON dossier.research_trial_id = trial.id
                JOIN analysis.strategy_revision trial_revision ON trial_revision.id = dossier.strategy_revision_id
                JOIN analysis.trial_result result ON result.research_trial_id = trial.id
                WHERE trial_revision.id = %s AND result.result_kind = 'validation'
                """,
                [first["strategy_revision_id"]],
            ).fetchone()
        assert research["status"] == "succeeded"
        assert all(value["passed"] for value in research["gates"].values())
        assert research["dsr"] is not None
        assert research["gate_count"] == 5
        with runtime.read() as connection:
            evidence = connection.execute(
                """SELECT count(*) AS source_count,
                          count(*) FILTER (WHERE manifest.evaluator_output_id IS NOT NULL
                                           AND manifest.evidence_hash = source.output_hash) AS bound_count
                   FROM analysis.research_evidence_manifest manifest
                   JOIN analysis.research_evaluator_output source ON source.id = manifest.evaluator_output_id
                   JOIN analysis.trial_result result ON result.id = manifest.trial_result_id
                   JOIN analysis.validation_dossier dossier ON dossier.research_trial_id = result.research_trial_id
                   WHERE dossier.strategy_revision_id = %s""",
                [first["strategy_revision_id"]],
            ).fetchone()
        assert evidence == {"source_count": 6, "bound_count": 6}
        with runtime.read() as connection:
            lineage = connection.execute(
                """
                SELECT hypothesis_id, experiment_family_id, research_trial_id,
                       validation_dossier_id, artifact_id, artifact_hash, input_hash
                FROM analysis.strategy_evaluation
                WHERE id = %s::uuid
                """,
                [first["strategy_evaluation_id"]],
            ).fetchone()
        assert lineage["hypothesis_id"] is not None
        assert lineage["experiment_family_id"] is not None
        assert lineage["research_trial_id"] is not None
        assert lineage["validation_dossier_id"] is not None
        assert lineage["artifact_id"].startswith("ticker-stock-alpha:")
        assert len(lineage["artifact_hash"]) == 64
        assert len(lineage["input_hash"]) == 64

        artifact = AnalysisRepository(runtime).qualified_stock_alpha_artifact(
            cutoff=cutoff, horizon="TACTICAL",
        )
        assert artifact["availability_status"] == "available"
        assert artifact["promotion_stage"] == "paper"
        assert artifact["cohort_path"]
        assert artifact["effective_sample_size"] >= 4
        assert artifact["calibration_metrics"]["brier_score"] is not None
        assert artifact["lower_confidence_net_utility_after_costs"] > 0
        with runtime.read() as connection:
            forecast = connection.execute(
                """SELECT id, forecast_distribution, generated_at, available_at
                   FROM analysis.strategy_forecast
                   WHERE strategy_revision_id = %s AND horizon = 'TACTICAL'
                   ORDER BY available_at DESC, id DESC LIMIT 1""",
                [first["strategy_revision_id"]],
            ).fetchone()
        assert forecast["id"] == artifact["strategy_forecast_id"]
        assert forecast["forecast_distribution"] == artifact["forecast"]["forecast_distribution"]
        assert forecast["generated_at"] <= cutoff
        assert forecast["available_at"] <= cutoff
    finally:
        runtime.close()


def test_incomplete_challenger_cannot_promote(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(seconds=5)
        _seed_universe_tape(runtime, cutoff, [f"S{index:02d}" for index in range(3)])
        result = run(
            runtime, _observations(3, cutoff), cutoff=cutoff,
            promote=True, authorization_mode="ADVISORY", min_train=4, min_cohort=4,
            universe_members=[f"S{index:02d}" for index in range(3)], control_results=_controls(),
        )
        assert result["complete"] is False
        assert result["promotion_evaluation_id"] is None
        assert result["promotion_stage"] == "challenger"
        with runtime.read() as connection:
            status = connection.execute(
                "SELECT status FROM analysis.strategy_revision WHERE id = %s",
                [result["strategy_revision_id"]],
            ).fetchone()["status"]
        assert status == "candidate"
    finally:
        runtime.close()


def test_production_path_missing_controls_is_visible_and_non_promotable(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(seconds=5)
        symbols = [f"S{index:02d}" for index in range(16)]
        _seed_universe_tape(runtime, cutoff, symbols, as_of=cutoff - timedelta(microseconds=2))
        result = run(
            runtime, _observations(16, cutoff), cutoff=cutoff,
            promote=True, authorization_mode="PAPER", min_train=4, fold_size=2,
            min_cohort=4, universe_members=symbols,
        )
        assert result["complete"] is False
        assert result["promotion_evaluation_id"] is None
        with runtime.read() as connection:
            control = connection.execute(
                """SELECT outcome FROM analysis.trial_result
                   WHERE research_trial_id = (
                       SELECT research_trial_id FROM analysis.validation_dossier
                       WHERE strategy_revision_id = %s
                   ) AND result_kind = 'negative_controls'""",
                [result["strategy_revision_id"]],
            ).fetchone()
        assert control["outcome"]["passed"] is False
    finally:
        runtime.close()


def test_exact_current_cutoff_retains_wall_clock_forecast_and_fails_closed(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC)
        symbols = [f"S{index:02d}" for index in range(16)]
        _seed_universe_tape(runtime, cutoff, symbols, as_of=cutoff - timedelta(microseconds=2))
        result = run(
            runtime, _observations(16, cutoff), cutoff=cutoff,
            promote=True, authorization_mode="PAPER", min_train=4, fold_size=2,
            min_cohort=4, universe_members=symbols, control_results=_controls(),
        )
        assert result["complete"] is True
        assert result["promotion_evaluation_id"] is None
        assert result["promotion_reason"] == "forecast_evidence_not_available_at_cutoff"
        with runtime.read() as connection:
            availability = connection.execute(
                "SELECT available_at FROM analysis.strategy_forecast WHERE strategy_revision_id = %s ORDER BY id LIMIT 1",
                [result["strategy_revision_id"]],
            ).fetchone()["available_at"]
        assert availability > cutoff
    finally:
        runtime.close()


def test_historical_cutoff_keeps_actual_forecast_availability_and_blocks_promotion(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) - timedelta(days=1)
        symbols = [f"S{index:02d}" for index in range(16)]
        _seed_universe_tape(runtime, cutoff, symbols)
        result = run(
            runtime, _observations(16, cutoff), cutoff=cutoff,
            promote=True, authorization_mode="PAPER", min_train=4, fold_size=2,
            min_cohort=4, universe_members=symbols, control_results=_controls(),
        )
        assert result["complete"] is True
        assert result["promotion_evaluation_id"] is None
        assert result["promotion_reason"] == "forecast_evidence_not_available_at_cutoff"
        with runtime.read() as connection:
            persisted = connection.execute(
                """SELECT forecast.available_at, dossier.status
                   FROM analysis.strategy_forecast forecast
                   JOIN analysis.strategy_evaluation evaluation ON evaluation.id = forecast.strategy_evaluation_id
                   JOIN analysis.validation_dossier dossier ON dossier.id = evaluation.validation_dossier_id
                   WHERE forecast.strategy_revision_id = %s
                   ORDER BY forecast.id LIMIT 1""",
                [result["strategy_revision_id"]],
            ).fetchone()
        assert persisted["available_at"] > cutoff
        assert persisted["status"] == "sealed"
    finally:
        runtime.close()


def test_canonical_pit_trend_feature_loads_for_training_and_live_inference(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        decision_at = datetime.now(UTC) + timedelta(seconds=1)
        cutoff = decision_at + timedelta(seconds=5)
        repository = AnalysisRepository(runtime)
        run_id = repository.start_run(
            "daily-trend", input_cutoff=decision_at - timedelta(minutes=1),
            code_version="test", inputs={"symbol": "PIT"},
            feature_versions={"daily_trend": FEATURE_VERSION},
        )
        with runtime.transaction() as connection:
            instrument_id = connection.execute(
                """
                INSERT INTO catalog.instrument (symbol, name, asset_class)
                VALUES ('PIT', 'PIT', 'equity') RETURNING id
                """
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO analysis.symbol_feature (
                    run_id, instrument_id, as_of, feature_set, feature_version,
                    momentum_5d, momentum_20d, relative_strength_20d,
                    relative_strength_60d, kaufman_er_20d,
                    trend_state, trend_confidence, volatility_state,
                    data_quality_status, reason_codes
                ) VALUES (%s, %s, %s, 'daily_trend', %s, 0.02, 0.04, 0.03,
                          0.06, 0.5, 'trend_up', 0.8, 'normal', 'complete', '{}')
                """,
                [run_id, instrument_id, decision_at - timedelta(minutes=2), FEATURE_VERSION],
            )
            decision_id = connection.execute(
                """
                INSERT INTO analysis.ticker_decision (
                    instrument_id, decision_revision, contract_version, as_of,
                    published_at, input_hash, code_version, experiment_id,
                    tactical, fundamental, capital_action, risk_policy
                ) VALUES (%s, 'pit-decision', 'test', %s, %s, %s, 'test', 'test',
                          '{}', '{}', '{}', '{}') RETURNING id
                """,
                [instrument_id, decision_at, decision_at, "a" * 64],
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO analysis.ticker_benchmark_snapshot (
                    benchmark_key, as_of, available_at, membership_hash,
                    member_count, source_id, exact_membership
                ) VALUES ('market-equity-etf', %s, %s, %s, 1, 'test', %s)
                """,
                [decision_at, decision_at, "b" * 64, Jsonb({"PIT": True})],
            )
            connection.execute(
                """
                INSERT INTO analysis.ticker_outcome (
                    ticker_decision_id, horizon, horizon_sessions, state,
                    selected_return, available_at, metadata
                ) VALUES (%s, 'TACTICAL', 5, 'resolved', 0.05, %s, %s)
                """,
                [
                    decision_id, decision_at + timedelta(seconds=1),
                    Jsonb({"cost_adjusted_selected_return": 0.049}),
                ],
            )
        repository.finish_run(run_id, "succeeded")

        observations = load_observations(runtime, cutoff=cutoff)
        assert len(observations) == 1
        assert observations[0]["features"]["feature_version"] == FEATURE_VERSION
        assert research_score(observations[0]["features"]) is not None
        feature = repository.stock_alpha_feature(
            "PIT", cutoff=decision_at, feature_version=FEATURE_VERSION,
        )
        assert feature is not None
        assert research_score(feature) == research_score(observations[0]["features"])
    finally:
        runtime.close()


def test_latest_oos_input_hash_mismatch_fails_closed(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(seconds=5)
        evaluation_cutoff = cutoff - timedelta(seconds=1)
        _seed_universe_tape(runtime, evaluation_cutoff, [f"S{index:02d}" for index in range(16)])
        result = run(
            runtime, _observations(16, evaluation_cutoff), cutoff=evaluation_cutoff,
            promote=True, authorization_mode="PAPER",
            min_train=4, fold_size=2, min_cohort=4,
            universe_members=[f"S{index:02d}" for index in range(16)], control_results=_controls(),
        )
        with runtime.transaction() as connection:
            connection.execute(
                """
                INSERT INTO analysis.strategy_evaluation (
                    strategy_revision_id, evaluation_type, evaluated_at,
                    period_start, period_end, verdict, metrics, evidence
                )
                SELECT strategy_revision_id, evaluation_type, %s,
                       period_start, period_end, verdict,
                       metrics || jsonb_build_object('input_hash', 'mismatch'), evidence
                FROM analysis.strategy_evaluation WHERE id = %s::uuid
                """,
                [cutoff, result["strategy_evaluation_id"]],
            )
        artifact = AnalysisRepository(runtime).qualified_stock_alpha_artifact(
            cutoff=cutoff, horizon="TACTICAL",
        )
        assert artifact["availability_status"] == "error"
        assert artifact["blockers"] == ["alpha_evaluation_lineage_mismatch"]
    finally:
        runtime.close()


def test_superseded_revision_replay_cannot_deactivate_current_champion(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(seconds=5)
        observations_a = _observations(16, cutoff)
        observations_b = _observations(17, cutoff)
        _seed_universe_tape(runtime, cutoff, [f"S{index:02d}" for index in range(16)], as_of=cutoff - timedelta(microseconds=2))
        first = run(
            runtime, observations_a, cutoff=cutoff, promote=True, authorization_mode="PAPER",
            min_train=4, fold_size=2, min_cohort=4,
            universe_members=[f"S{index:02d}" for index in range(16)], control_results=_controls(),
        )
        _seed_universe_tape(runtime, cutoff, [f"S{index:02d}" for index in range(17)], as_of=cutoff - timedelta(microseconds=1))
        second = run(
            runtime, observations_b, cutoff=cutoff, promote=True, authorization_mode="PAPER",
            min_train=4, fold_size=2, min_cohort=4,
            universe_members=[f"S{index:02d}" for index in range(17)], control_results=_controls(),
        )
        with pytest.raises(ValueError, match="submitted universe|superseded stock-alpha"):
            run(
                runtime, observations_a, cutoff=cutoff, promote=True, authorization_mode="PAPER",
                min_train=4, fold_size=2, min_cohort=4,
                universe_members=[f"S{index:02d}" for index in range(16)], control_results=_controls(),
            )
        with runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT id, status FROM analysis.strategy_revision
                WHERE strategy_key = 'ticker-stock-alpha' ORDER BY revision
                """
            ).fetchall()
        assert [(row["id"], row["status"]) for row in rows] == [
            (first["strategy_revision_id"], "superseded"),
            (second["strategy_revision_id"], "active"),
        ]
    finally:
        runtime.close()
