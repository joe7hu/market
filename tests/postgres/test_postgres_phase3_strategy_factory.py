from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from investment_panel.analysis.stock_alpha import content_hash
from investment_panel.core.strategy_factory import default_strategy_registry
from investment_panel.database.migrations import downgrade_database, upgrade_database
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.strategy_factory import StrategyFactoryRepository


def test_phase3_migration_exposes_bounded_registry_contract(migrated_postgres_dsn: str) -> None:
    with psycopg.connect(migrated_postgres_dsn) as connection:
        tables = connection.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema = 'analysis'
                 AND table_name IN ('strategy_manifest', 'strategy_pnl_tape',
                                    'strategy_monitoring_evidence', 'strategy_comparison')
               ORDER BY table_name""",
        ).fetchall()
        views = connection.execute(
            """SELECT table_name FROM information_schema.views
               WHERE table_schema = 'analysis'
                 AND table_name IN ('strategy_registry', 'strategy_trial_accounting')
               ORDER BY table_name""",
        ).fetchall()
        assert [row[0] for row in tables] == [
            "strategy_comparison", "strategy_manifest", "strategy_monitoring_evidence", "strategy_pnl_tape",
        ]
        assert [row[0] for row in views] == ["strategy_registry", "strategy_trial_accounting"]
        columns = connection.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = 'analysis' AND table_name = 'strategy_revision'
                 AND column_name IN ('mechanism_class', 'source_definition_version', 'promotability',
                                     'actionability', 'p3_enabled')
               ORDER BY column_name""",
        ).fetchall()
        assert [row[0] for row in columns] == [
            "actionability", "mechanism_class", "p3_enabled", "promotability", "source_definition_version",
        ]


def test_phase3_downgrade_removes_registry_views_before_related_objects(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    downgrade_database(postgres_dsn, "20260902_0069")
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM information_schema.views WHERE table_schema = 'analysis' AND table_name IN ('strategy_registry', 'strategy_trial_accounting')",
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'analysis' AND table_name IN ('strategy_manifest', 'strategy_pnl_tape', 'strategy_monitoring_evidence', 'strategy_comparison')",
        ).fetchone()[0] == 0


def test_phase3_evidence_is_immutable_and_martingale_is_not_promotable(
    migrated_postgres_dsn: str,
) -> None:
    with psycopg.connect(migrated_postgres_dsn) as connection:
        revision = connection.execute(
            """INSERT INTO analysis.strategy_revision
               (strategy_key, revision, name, status, parameters, mechanism_class,
                economic_mechanism, falsification_rule, source_definition_version,
                promotability, actionability, p3_enabled, authority_group)
               VALUES ('phase3-test_v1', 1, 'Phase 3 test', 'candidate', %s, 'gap_regime',
                       'test mechanism', 'test falsification', 'phase3-test.v1',
                       'standard', 'daily_research', true, 'phase3-test')
               RETURNING id""",
            [Jsonb({"paper_only": True})],
        ).fetchone()[0]
        now = datetime.now(UTC)
        connection.execute(
            """INSERT INTO analysis.strategy_manifest
               (strategy_revision_id, source_definition_version, source_manifest,
                data_manifest, cost_manifest, capacity_manifest, failure_manifest, manifest_hash)
               VALUES (%s, 'phase3-test.v1', %s, %s, %s, %s, %s, %s)""",
            [revision, *[Jsonb({"complete": key}) for key in ("source", "data", "cost", "capacity", "failure")], "a" * 64],
        )
        connection.execute("SAVEPOINT phase3_immutable")
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            connection.execute(
                "UPDATE analysis.strategy_manifest SET manifest_hash = %s WHERE strategy_revision_id = %s",
                ["c" * 64, revision],
            )
        connection.execute("ROLLBACK TO SAVEPOINT phase3_immutable")
        martingale = connection.execute(
            """INSERT INTO analysis.strategy_revision
               (strategy_key, revision, name, status, parameters, mechanism_class,
                economic_mechanism, falsification_rule, source_definition_version,
                promotability, actionability, p3_enabled, authority_group)
               VALUES ('martingale_v1', 1, 'Martingale', 'candidate', %s, 'gap_regime',
                       'negative control', 'never promote', 'martingale.v1',
                       'negative_control', 'research_only', true, 'martingale-test')
               RETURNING id""",
            [Jsonb({"paper_only": True})],
        ).fetchone()[0]
        connection.execute("SAVEPOINT martingale_promotion")
        with pytest.raises(psycopg.errors.RaiseException, match="permanent research-only"):
            connection.execute("UPDATE analysis.strategy_revision SET status = 'active' WHERE id = %s", [martingale])
        connection.execute("ROLLBACK TO SAVEPOINT martingale_promotion")
        connection.execute("SAVEPOINT martingale_variant")
        with pytest.raises(psycopg.errors.RaiseException, match="permanent research-only"):
            connection.execute(
                """INSERT INTO analysis.strategy_revision
                   (strategy_key, revision, name, status, parameters, mechanism_class,
                    economic_mechanism, falsification_rule, source_definition_version,
                    promotability, actionability, authority_group)
                   VALUES ('martingale_v2', 2, 'Martingale v2', 'candidate', %s, 'gap_regime',
                           'negative control', 'never promote', 'martingale.v2',
                           'standard', 'daily_research', 'martingale-v2')""",
                [Jsonb({"paper_only": True})],
            )
        connection.execute("ROLLBACK TO SAVEPOINT martingale_variant")
        connection.execute("SAVEPOINT martingale_family_variant")
        with pytest.raises(psycopg.errors.RaiseException, match="permanent research-only"):
            connection.execute(
                """INSERT INTO analysis.strategy_revision
                   (strategy_key, revision, name, status, parameters, mechanism_class,
                    economic_mechanism, falsification_rule, source_definition_version,
                    strategy_family, promotability, actionability, authority_group)
                   VALUES ('ordinary_control_v1', 1, 'Ordinary control', %s, %s, 'trend_underreaction',
                           'negative control', 'never promote', 'ordinary.v1', 'martingale_v2',
                           'standard', 'daily_research', 'martingale-family-test')""",
                ["candidate", Jsonb({"paper_only": True})],
            )
        connection.execute("ROLLBACK TO SAVEPOINT martingale_family_variant")


def test_phase3_repository_rejects_conflicting_registration_identity(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        repository = StrategyFactoryRepository(runtime)
        spec = default_strategy_registry().resolve("daily_trend_underreaction_v1")
        repository.register(spec)
        with pytest.raises(ValueError, match="identity conflicts"):
            repository.register(spec.model_copy(update={"name": "conflicting name"}))
        with pytest.raises(psycopg.errors.RaiseException, match="complete PIT denominator outcomes"):
            repository.promote(repository.register(spec))
    finally:
        runtime.close()


def test_phase3_repository_resolves_only_postgres_registered_strategy(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        repository = StrategyFactoryRepository(runtime)
        spec = default_strategy_registry().resolve("daily_trend_underreaction_v1")
        revision_id = repository.register(spec)
        resolved = repository.resolve(spec.strategy_key)
        assert revision_id > 0
        assert resolved == spec
        with pytest.raises(KeyError, match="PostgreSQL"):
            repository.resolve("daily_trend_underreaction_v1", revision=99)
    finally:
        runtime.close()


def test_phase3_evidence_requires_canonical_pit_lineage_and_computed_claims(
    migrated_postgres_dsn: str,
) -> None:
    cutoff = datetime.now(UTC) + timedelta(days=1)
    with psycopg.connect(migrated_postgres_dsn) as connection:
        instruments = [
            connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES (%s, %s, 'equity') RETURNING id",
                [symbol, symbol],
            ).fetchone()[0]
            for symbol in ("P3A", "P3B")
        ]
        revisions = []
        trials = []
        results = []
        manifests = []
        for index, key in enumerate(("p3-champion_v1", "p3-challenger_v1"), start=1):
            revision = connection.execute(
                """INSERT INTO analysis.strategy_revision
                   (strategy_key, revision, name, status, parameters, mechanism_class,
                    economic_mechanism, falsification_rule, source_definition_version,
                    strategy_family, promotability, actionability, p3_enabled, authority_group)
                   VALUES (%s, 1, %s, 'candidate', %s, 'trend_underreaction',
                           'test mechanism', 'test falsification', %s, 'legacy',
                           'standard', 'daily_research', true, %s) RETURNING id""",
                [key, key, Jsonb({"paper_only": True}), f"{key}.v1", key],
            ).fetchone()[0]
            hypothesis = connection.execute(
                """INSERT INTO analysis.hypothesis
                   (hypothesis_key, statement, mechanism_class, falsification, input_hash)
                   VALUES (%s, 'test', 'trend_underreaction', 'test', %s) RETURNING id""",
                [f"{key}-hypothesis", str(index) * 64],
            ).fetchone()[0]
            family = connection.execute(
                """INSERT INTO analysis.experiment_family
                   (hypothesis_id, family_key, name, input_hash)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                [hypothesis, f"{key}-family", key, str(index + 2) * 64],
            ).fetchone()[0]
            trial = connection.execute(
                """INSERT INTO analysis.research_trial
                   (experiment_family_id, trial_key, input_cutoff, code_version, input_hash, available_at)
                   VALUES (%s, %s, %s, 'test', %s, now()) RETURNING id""",
                [family, f"{key}-trial", cutoff, str(index + 3) * 64],
            ).fetchone()[0]
            expected_members = Jsonb([str(instrument) for instrument in instruments])
            connection.execute(
                """INSERT INTO analysis.trial_universe_manifest
                   (research_trial_id, cutoff, expected_member_count, expected_members, manifest_hash, available_at)
                   VALUES (%s, %s, %s, %s,
                           encode(public.digest(convert_to(replace(%s::JSONB::TEXT, ' ', ''), 'UTF8'), 'sha256'), 'hex'), now())""",
                [trial, cutoff, len(instruments), expected_members, expected_members],
            )
            for rank, instrument in enumerate(instruments, start=1):
                connection.execute(
                    """INSERT INTO analysis.universe_observation
                       (research_trial_id, instrument_id, cutoff, eligible, rank, observed_at, available_at, input_hash, outcome)
                       VALUES (%s, %s, %s, true, %s, now(), now(), %s, %s)""",
                    [trial, instrument, cutoff, rank, str(index + 3) * 64, Jsonb({"net_return": 0.1})],
                )
            result_hash = str(index + 4) * 64
            factor_exposure = {"market_beta": 0.1}
            factor_exposure_hash = connection.execute(
                "SELECT analysis.phase3_json_hash(%s::JSONB)", [Jsonb(factor_exposure)],
            ).fetchone()[0]
            neutralized_result_hash = connection.execute(
                """SELECT analysis.phase3_json_hash(jsonb_build_object(
                           'factor_exposure', %s::JSONB, 'neutralized', true, 'result_hash', %s::TEXT))""",
                [Jsonb(factor_exposure), result_hash],
            ).fetchone()[0]
            result = connection.execute(
                """INSERT INTO analysis.trial_result
                   (research_trial_id, result_kind, observed_at, available_at, input_hash, outcome)
                   VALUES (%s, 'validation', now(), now(), %s, %s) RETURNING id, input_hash""",
                [trial, result_hash, Jsonb({
                    "passed": True, "neutralized": True, "factor_exposure": factor_exposure,
                    "factor_exposure_hash": factor_exposure_hash, "factor_result_hash": result_hash,
                    "neutralized_result_hash": neutralized_result_hash,
                })],
            ).fetchone()
            connection.execute(
                """INSERT INTO analysis.strategy_manifest
                   (strategy_revision_id, source_definition_version, source_manifest,
                    data_manifest, cost_manifest, capacity_manifest, failure_manifest, manifest_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                [revision, f"{key}.v1", *[Jsonb({"part": part}) for part in ("source", "data", "cost", "capacity", "failure")], "f" * 64],
            )
            manifest = connection.execute(
                "SELECT manifest_hash FROM analysis.strategy_manifest WHERE strategy_revision_id = %s", [revision],
            ).fetchone()[0]
            revisions.append(revision)
            trials.append(trial)
            results.append(result)
            manifests.append(manifest)
        forecast_ids = []
        for index, revision in enumerate(revisions):
            forecast_ids.append([])
            for instrument in instruments:
                episode_id = f"p3-episode-{index}-{instrument}"
                forecast_id = connection.execute(
                    """WITH payload AS (SELECT now() AS generated_at)
                       INSERT INTO analysis.strategy_forecast
                           (id, strategy_revision_id, instrument_id, opportunity_episode_id, target, horizon,
                            forecast_value, model_artifact_id, artifact_hash, input_hash, as_of, input_cutoff,
                            generated_at, available_at, research_trial_id, trial_result_id,
                            universe_manifest_hash, result_hash)
                       SELECT 'forecast:strategy-forecast:' || left(encode(digest(
                           jsonb_build_array(
                               'strategy-forecast.v1',
                               (SELECT upper(symbol) FROM catalog.instrument WHERE id = %s),
                               %s::TEXT, %s::TEXT, NULL::TEXT, 'return', 'DAILY',
                               analysis.canonical_forecast_number(0.1::DOUBLE PRECISION),
                               analysis.canonical_forecast_number(NULL::DOUBLE PRECISION),
                               analysis.canonical_forecast_number(NULL::DOUBLE PRECISION), '', NULL::TEXT,
                               'p3-model', %s::TEXT, %s::TEXT,
                               to_char(%s::TIMESTAMPTZ AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00',
                               to_char(%s::TIMESTAMPTZ AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00',
                               to_char(payload.generated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00',
                               to_char(payload.generated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00'
                           )::TEXT, 'sha256'), 'hex'), 32),
                           %s, %s, %s, 'return', 'DAILY', 0.1, 'p3-model', %s, %s,
                           %s, %s, payload.generated_at, payload.generated_at, %s, %s, %s, %s
                       FROM payload
                       RETURNING id""",
                    [instrument, episode_id, revision, "a" * 64, "b" * 64, cutoff, cutoff,
                     revision, instrument, episode_id, "a" * 64, "b" * 64, cutoff, cutoff,
                     trials[index], results[index][0], manifests[index], results[index][1]],
                ).fetchone()[0]
                forecast_ids[index].append(forecast_id)
                connection.execute(
                    """INSERT INTO analysis.strategy_pnl_tape
                       (strategy_revision_id, instrument_id, pnl_date, strategy_forecast_id,
                        research_trial_id, trial_result_id, universe_manifest_hash, result_hash,
                        input_cutoff, gross_return, cost, net_return, tail_return, regime, observed_at, available_at, input_hash, metadata)
                       VALUES (%s, %s, CASE WHEN %s::BIGINT = %s::BIGINT THEN '2026-09-02'::DATE ELSE '2026-09-03'::DATE END, %s, %s, %s, %s, %s, %s,
                               CASE WHEN %s::BIGINT = %s::BIGINT THEN 0.1 ELSE 0.2 END, 0.01,
                               CASE WHEN %s::BIGINT = %s::BIGINT THEN -0.09 ELSE -0.18 END,
                               CASE WHEN %s::BIGINT = %s::BIGINT THEN -0.08 ELSE -0.17 END,
                               'normal', now(), now(), %s, %s)""",
                        [revision, instrument, instruments[0], instrument, forecast_id, trials[index], results[index][0], manifests[index], results[index][1], cutoff, instruments[0], instrument, instruments[0], instrument, instruments[0], instrument, "0" * 64, Jsonb({"intended_notional": 10000, "unwind_cost": 25})],
                )
        connection.execute("SAVEPOINT invalid_pnl_lineage")
        with pytest.raises(psycopg.errors.RaiseException, match="invalid canonical lineage"):
            connection.execute(
                """INSERT INTO analysis.strategy_pnl_tape
                   (strategy_revision_id, instrument_id, pnl_date, strategy_forecast_id,
                    research_trial_id, trial_result_id, universe_manifest_hash, result_hash,
                    input_cutoff, gross_return, cost, net_return, observed_at, available_at, input_hash, metadata)
                   VALUES (%s, %s, '2026-09-03', %s, %s, %s, %s, %s, %s, 0.1, 0.01, 0.09, now(), now(), %s, %s)""",
                [revisions[0], instruments[0], forecast_ids[1][0], trials[0], results[0][0], manifests[0], results[0][1], cutoff, "0" * 64, Jsonb({"intended_notional": 10000, "unwind_cost": 25})],
            )
        connection.execute("ROLLBACK TO SAVEPOINT invalid_pnl_lineage")
        connection.execute("SAVEPOINT label_only_monitoring")
        with pytest.raises(psycopg.errors.RaiseException, match="(linked strategy revisions|typed canonical content)"):
            connection.execute(
                """INSERT INTO analysis.strategy_monitoring_evidence
                   (strategy_revision_id, research_trial_id, trial_result_id, universe_manifest_hash,
                    result_hash, evidence_kind, input_cutoff, observed_at, available_at, input_hash, metrics, evidence)
                   VALUES (%s, %s, %s, %s, %s, 'capacity', %s, now(), now(), %s, %s, %s)""",
                [revisions[0], trials[0], results[0][0], manifests[0], results[0][1], cutoff, "0" * 64,
                 Jsonb({"max_cost": 0.01}), Jsonb({"evidence_kind": "capacity"})],
            )
        connection.execute("ROLLBACK TO SAVEPOINT label_only_monitoring")
        connection.execute("SAVEPOINT future_result_monitoring")
        with pytest.raises(psycopg.errors.RaiseException, match="PIT lineage"):
            result_available_at = connection.execute(
                "SELECT available_at FROM analysis.trial_result WHERE id = %s", [results[0][0]],
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO analysis.strategy_monitoring_evidence
                   (strategy_revision_id, research_trial_id, trial_result_id, universe_manifest_hash,
                    result_hash, evidence_kind, input_cutoff, observed_at, available_at, input_hash, metrics, evidence)
                   VALUES (%s, %s, %s, %s, %s, 'capacity', %s, now(), now(), %s, %s, %s)""",
                [revisions[0], trials[0], results[0][0], manifests[0], results[0][1],
                    result_available_at - timedelta(microseconds=1), "1" * 64, Jsonb({}),
                    Jsonb({"linked_strategy_revision_ids": revisions})],
            )
        connection.execute("ROLLBACK TO SAVEPOINT future_result_monitoring")
        canonical = connection.execute(
            """WITH tape AS (
                     SELECT tape.*, forecast.input_hash AS forecast_input_hash
                       FROM analysis.strategy_pnl_tape tape
                       JOIN analysis.strategy_forecast forecast ON forecast.id = tape.strategy_forecast_id
                      WHERE tape.strategy_revision_id = %s
                        AND tape.research_trial_id = %s
                        AND tape.trial_result_id = %s
                        AND tape.available_at <= %s
                        AND forecast.available_at <= %s
                 ), weighted AS (
                     SELECT abs(net_return) / NULLIF(sum(abs(net_return)) OVER (), 0) AS weight FROM tape
                 ), peer AS (
                     SELECT peer.* FROM analysis.strategy_pnl_tape peer
                      WHERE peer.strategy_revision_id = %s
                        AND peer.available_at <= %s
                 ), pairs AS (
                     SELECT base.*, peer.id AS peer_id, peer.net_return AS peer_net_return, peer.tail_return AS peer_tail_return,
                            peer.input_hash AS peer_input_hash
                       FROM tape base JOIN peer
                         ON peer.instrument_id = base.instrument_id AND peer.pnl_date = base.pnl_date
                 ), regimes AS (
                     SELECT regime, avg(net_return) AS avg_return, count(*) AS sample_count,
                            power(sum(abs(net_return)), 2) / NULLIF(sum(power(abs(net_return), 2)), 0) AS effective_sample_size
                       FROM tape WHERE regime IS NOT NULL GROUP BY regime
                 )
                 SELECT count(*), count(*) FILTER (WHERE tail_return IS NOT NULL), count(DISTINCT instrument_id),
                        count(DISTINCT regime) FILTER (WHERE regime IS NOT NULL), count(DISTINCT pnl_date),
                        corr(peer_net_return, net_return),
                        corr(peer_net_return, net_return) FILTER (WHERE peer_net_return < 0 AND net_return < 0),
                        corr(peer_tail_return, tail_return) FILTER (WHERE peer_tail_return IS NOT NULL AND tail_return IS NOT NULL),
                        (SELECT sum(power(weight, 2)) FROM weighted),
                        power(sum(abs(net_return) + abs(peer_net_return)), 2) / NULLIF(sum(power(abs(net_return) + abs(peer_net_return), 2)), 0),
                        power(sum(abs(tail_return) + abs(peer_tail_return)) FILTER (WHERE tail_return IS NOT NULL AND peer_tail_return IS NOT NULL), 2) / NULLIF(sum(power(abs(tail_return) + abs(peer_tail_return), 2)) FILTER (WHERE tail_return IS NOT NULL AND peer_tail_return IS NOT NULL), 0),
                        (SELECT regr_slope(net_return, extract(epoch FROM pnl_date::timestamp)) FROM tape),
                        (SELECT COALESCE(jsonb_object_agg(regime, jsonb_build_object('net_return', avg_return, 'sample_size', sample_count, 'effective_sample_size', effective_sample_size)), '{}'::jsonb) FROM regimes),
                        COALESCE(jsonb_agg(input_hash::TEXT ORDER BY id), '[]'::jsonb),
                        COALESCE(jsonb_agg(forecast_input_hash::TEXT ORDER BY id), '[]'::jsonb),
                        (SELECT COALESCE(jsonb_agg(peer_input_hash::TEXT ORDER BY peer_id), '[]'::jsonb) FROM pairs),
                        (SELECT COALESCE(jsonb_agg(result_hash::TEXT ORDER BY result_hash), '[]'::jsonb) FROM (SELECT DISTINCT result_hash FROM peer) peer_results),
                        (SELECT sum((metadata->>'intended_notional')::DOUBLE PRECISION) FROM tape),
                        (SELECT sum((metadata->>'unwind_cost')::DOUBLE PRECISION) FROM tape),
                        (SELECT stddev_pop(avg_return) FROM regimes)
                   FROM pairs""",
            [revisions[0], trials[0], results[0][0], cutoff, cutoff, revisions[1], cutoff],
        ).fetchone()
        metric_names = {
            "correlation": ("normal_correlation", canonical[5], canonical[0], canonical[9]),
            "tail_correlation": ("tail_correlation", canonical[7], canonical[1], canonical[10]),
            "crowding": ("crowding_hhi", canonical[8], canonical[2], 1 / canonical[8]),
            "capacity": ("capacity_cost_ratio", (0.02 + canonical[18]) / canonical[17], canonical[0], canonical[0]),
            "decay": ("decay_slope", canonical[11], canonical[4], canonical[4]),
            "regime": ("regime_dispersion", canonical[19], canonical[0], canonical[0]),
        }
        evidence_time = datetime.now(UTC)
        for evidence_kind in ("correlation", "tail_correlation", "crowding", "capacity", "decay", "regime"):
            metric_name, metric_value, sample_size, effective_sample_size = metric_names[evidence_kind]
            metrics = {"evidence_kind": evidence_kind, "metric_name": metric_name, "sample_size": sample_size, "effective_sample_size": effective_sample_size, metric_name: metric_value}
            evidence = {
                "evidence_kind": evidence_kind, "metric_name": metric_name, "metric_value": metric_value,
                "sample_size": sample_size, "effective_sample_size": effective_sample_size,
                "trial_result_hash": results[0][1], "universe_manifest_hash": manifests[0],
                "pnl_input_hashes": canonical[13], "forecast_input_hashes": canonical[14],
                "normal_correlation": canonical[5], "downside_correlation": canonical[6], "tail_correlation": canonical[7],
                "crowding_hhi": canonical[8], "intended_notional": canonical[17], "unwind_cost": canonical[18],
                "capacity_cost_ratio": (0.02 + canonical[18]) / canonical[17], "decay_slope": canonical[11],
                "regime_dispersion": canonical[19], "regime_slices": canonical[12],
                "linked_strategy_revision_ids": revisions, "linked_pnl_input_hashes": canonical[15],
                "linked_result_hashes": canonical[16], "input_cutoff": cutoff.isoformat(),
                "observed_at": evidence_time.isoformat(), "available_at": evidence_time.isoformat(),
            }
            connection.execute(
                """INSERT INTO analysis.strategy_monitoring_evidence
                   (strategy_revision_id, research_trial_id, trial_result_id, universe_manifest_hash,
                    result_hash, evidence_kind, input_cutoff, observed_at, available_at, input_hash, metrics, evidence)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [revisions[0], trials[0], results[0][0], manifests[0], results[0][1], evidence_kind,
                 cutoff, evidence_time, evidence_time, "0" * 64, Jsonb(metrics), Jsonb(evidence)],
            )
        monitoring = connection.execute(
            "SELECT metrics, lineage FROM analysis.strategy_monitoring_evidence WHERE strategy_revision_id = %s ORDER BY evidence_kind LIMIT 1",
            [revisions[0]],
        ).fetchone()
        assert monitoring[1]["generated_by"] == "postgresql"
        assert monitoring[0]["pnl_observation_count"] == 2
        connection.execute(
            """INSERT INTO analysis.strategy_comparison
               (champion_revision_id, challenger_revision_id, champion_trial_id, challenger_trial_id,
                champion_result_id, challenger_result_id, champion_result_hash, challenger_result_hash,
                champion_manifest_hash, challenger_manifest_hash, input_cutoff, observed_at, available_at,
                input_hash, distinctness, explanation, metrics)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now(), %s,
                       'distinct', 'caller claim', %s)""",
            [revisions[0], revisions[1], trials[0], trials[1], results[0][0], results[1][0], results[0][1], results[1][1],
             manifests[0], manifests[1], cutoff, "0" * 64, Jsonb({"caller_forge": True})],
        )
        comparison = connection.execute(
            "SELECT distinctness, metrics FROM analysis.strategy_comparison ORDER BY id DESC LIMIT 1",
        ).fetchone()
        assert comparison[0] == "blocked"
        assert comparison[1]["generated_by"] == "postgresql"
        blocked_results = [
            connection.execute(
                """INSERT INTO analysis.trial_result
                   (research_trial_id, result_kind, result_version, observed_at, available_at, input_hash, outcome)
                   VALUES (%s, 'validation', 2, now(), now(), %s, %s) RETURNING id, input_hash""",
                [trial, str(index + 7) * 64, Jsonb({"passed": True})],
            ).fetchone()
            for index, trial in enumerate(trials, start=1)
        ]
        connection.execute(
            """INSERT INTO analysis.strategy_comparison
               (champion_revision_id, challenger_revision_id, champion_trial_id, challenger_trial_id,
                champion_result_id, challenger_result_id, champion_result_hash, challenger_result_hash,
                champion_manifest_hash, challenger_manifest_hash, input_cutoff, observed_at, available_at,
                input_hash, distinctness, explanation, metrics)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now(), %s,
                       'distinct', 'caller claim', %s)""",
            [revisions[0], revisions[1], trials[0], trials[1], blocked_results[0][0], blocked_results[1][0],
             blocked_results[0][1], blocked_results[1][1], manifests[0], manifests[1], cutoff, "0" * 64,
             Jsonb({"caller_forge": True})],
        )
        blocked_comparison = connection.execute(
            "SELECT distinctness FROM analysis.strategy_comparison WHERE champion_result_id = %s",
            [blocked_results[0][0]],
        ).fetchone()[0]
        assert blocked_comparison == "blocked"
        forged_result = connection.execute(
            """INSERT INTO analysis.trial_result
               (research_trial_id, result_kind, result_version, observed_at, available_at, input_hash, outcome)
               VALUES (%s, 'validation', 3, now(), now(), %s, %s) RETURNING id, input_hash""",
            [trials[0], "8" * 64, Jsonb({
                "passed": True, "neutralized": True, "factor_exposure": {"market_beta": 0.1},
                "factor_exposure_hash": "0" * 64, "factor_result_hash": "8" * 64,
                "neutralized_result_hash": "0" * 64, "factor_evidence_producer": "caller",
            })],
        ).fetchone()
        connection.execute(
            """INSERT INTO analysis.strategy_comparison
               (champion_revision_id, challenger_revision_id, champion_trial_id, challenger_trial_id,
                champion_result_id, challenger_result_id, champion_result_hash, challenger_result_hash,
                champion_manifest_hash, challenger_manifest_hash, input_cutoff, observed_at, available_at,
                input_hash, distinctness, explanation, metrics)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now(), %s,
                       'distinct', 'caller claim', %s)""",
            [revisions[0], revisions[1], trials[0], trials[1], forged_result[0], results[1][0], forged_result[1], results[1][1],
             manifests[0], manifests[1], cutoff, "2" * 64, Jsonb({"caller_forge": True})],
        )
        assert connection.execute(
            "SELECT distinctness FROM analysis.strategy_comparison WHERE champion_result_id = %s",
            [forged_result[0]],
        ).fetchone()[0] == "blocked"


def test_phase3_trial_accounting_requires_full_outcome_denominator(migrated_postgres_dsn: str) -> None:
    cutoff = datetime.now(UTC) + timedelta(days=1)
    with psycopg.connect(migrated_postgres_dsn) as connection:
        instrument = connection.execute(
            "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('P3T', 'Phase 3 Test', 'equity') RETURNING id",
        ).fetchone()[0]
        second_instrument = connection.execute(
            "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('P3U', 'Phase 3 Test 2', 'equity') RETURNING id",
        ).fetchone()[0]
        hypothesis = connection.execute(
            """INSERT INTO analysis.hypothesis (hypothesis_key, statement, mechanism_class, falsification, input_hash)
               VALUES ('p3-hypothesis', 'test', 'gap_regime', 'test', %s) RETURNING id""",
            ["1" * 64],
        ).fetchone()[0]
        family = connection.execute(
            """INSERT INTO analysis.experiment_family (hypothesis_id, family_key, name, input_hash)
               VALUES (%s, 'p3-family', 'Phase 3 family', %s) RETURNING id""",
            [hypothesis, "2" * 64],
        ).fetchone()[0]
        trial = connection.execute(
            """INSERT INTO analysis.research_trial
               (experiment_family_id, trial_key, input_cutoff, code_version, input_hash, available_at)
               VALUES (%s, 'p3-trial', %s, 'test', %s, now()) RETURNING id""",
            [family, cutoff, "3" * 64],
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO analysis.trial_universe_manifest
               (research_trial_id, cutoff, expected_member_count, expected_members, manifest_hash)
               VALUES (%s, %s, 2, %s, %s)""",
            [trial, cutoff, Jsonb([str(instrument), str(second_instrument)]), content_hash([str(instrument), str(second_instrument)])],
        )
        connection.execute(
            """INSERT INTO analysis.universe_observation
               (research_trial_id, instrument_id, cutoff, eligible, rank, observed_at, available_at, input_hash, outcome)
               VALUES (%s, %s, %s, true, 1, now(), now(), %s, %s)""",
            [trial, instrument, cutoff, "3" * 64, Jsonb({"net_return": 0.1})],
        )
        row = connection.execute(
            "SELECT denominator_complete, observed_member_count, outcome_member_count FROM analysis.strategy_trial_accounting WHERE research_trial_id = %s",
            [trial],
        ).fetchone()
        assert tuple(row) == (False, 1, 1)
        connection.execute(
            """INSERT INTO analysis.universe_observation
               (research_trial_id, instrument_id, cutoff, eligible, rank, observed_at, available_at, input_hash, outcome)
               VALUES (%s, %s, %s, true, 2, now(), now(), %s, %s)""",
            [trial, second_instrument, cutoff, "3" * 64, Jsonb({"net_return": -0.1})],
        )
        assert connection.execute(
            "SELECT analysis.research_trial_p3_denominator_complete(%s)", [trial],
        ).fetchone()[0] is True
