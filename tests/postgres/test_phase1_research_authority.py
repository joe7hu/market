from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from investment_panel.core.decision.alpha import build_strategy_forecast
from investment_panel.analysis.stock_alpha import content_hash
from investment_panel.database.migrations import upgrade_database


def test_phase1_trial_dossier_forecast_and_universe_authority(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MARKET_RESEARCH_EVALUATOR_SIGNING_KEY", "phase1-test-signing-key")
    upgrade_database(postgres_dsn)
    cutoff = datetime.now(UTC) + timedelta(days=1)
    with psycopg.connect(postgres_dsn) as connection:
        instrument = connection.execute(
            "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('P1T', 'Phase 1 Test', 'equity') RETURNING id"
        ).fetchone()[0]
        hypothesis = connection.execute(
            """INSERT INTO analysis.hypothesis
               (hypothesis_key, statement, mechanism_class, falsification, input_hash)
               VALUES ('p1-hypothesis', 'test', 'quality', 'negative control', %s)
               RETURNING id""",
            ["1" * 64],
        ).fetchone()[0]
        family = connection.execute(
            """INSERT INTO analysis.experiment_family
               (hypothesis_id, family_key, name, input_hash)
               VALUES (%s, 'p1-family', 'Phase 1 family', %s) RETURNING id""",
            [hypothesis, "2" * 64],
        ).fetchone()[0]
        successful_checks = {
            "pit": {"passed": True, "domain_valid": True, "future_count": 0, "observed_count": 1},
            "denominator": {"passed": True, "domain_valid": True, "expected_count": 1, "observed_count": 1},
            "attempt_manifest": {"passed": True, "domain_valid": True, "expected_count": 2, "completed_count": 2},
            "negative_controls": {"passed": True, "domain_valid": True, "controls_present": True, "randomized_sample_count": 1, "white_noise_sample_count": 1, "randomized_label_samples": [0.0], "white_noise_samples": [0.0]},
            "mechanism": {"passed": True, "domain_valid": True, "evidence_count": 1, "evidence_samples": [0.1], "mechanism_class": "quality", "falsification_rule": "negative"},
            "parameter_stability": {"passed": True, "domain_valid": True, "sample_size": 3, "samples": [0.1, 0.1, 0.1]},
            "neutralization": {"passed": True, "domain_valid": True, "result_exists": True, "sample_size": 1, "samples": [0.1]},
            "combinatorial_paths": {"passed": True, "domain_valid": True, "path_count": 1, "path_records": [{"path_id": "manual-0", "test_folds": [0], "train_folds": [1], "metrics": {"domain_valid": True, "sample_size": 1, "mean_return": 0.1, "psr": 0.9, "p_value": 0.1, "fit_train_count": 1, "evaluated_test_count": 1}}]},
            "robustness": {"passed": True, "domain_valid": True, "negative_controls": {"passed": True, "domain_valid": True, "controls_present": True, "randomized_sample_count": 1, "white_noise_sample_count": 1}, "parameter_stability": {"passed": True, "domain_valid": True, "sample_size": 3}, "combinatorial_paths": {"passed": True, "domain_valid": True, "path_count": 1, "path_records": [{"path_id": "manual-0", "test_folds": [0], "train_folds": [1], "metrics": {"domain_valid": True, "sample_size": 1, "mean_return": 0.1, "psr": 0.9, "p_value": 0.1, "fit_train_count": 1, "evaluated_test_count": 1}}]}},
            "multiple_testing": {"psr": 0.9, "dsr": 0.9, "pbo": 0.0, "data_snooping_probability": 0.1, "fdr_q_value": 0.1, "domain_valid": True, "paths_domain_valid": True, "p_values_domain_valid": True, "trials_tested": 2, "sample_size": 1, "path_count": 1, "p_value_count": 1, "path_returns": [0.1], "p_values": [0.1]},
            "cost_capacity": {"passed": True, "domain_valid": True, "multiples": {"1x": {"net_return": 0.1, "capacity": 1.0}, "2x": {"net_return": 0.09, "capacity": 0.5}, "3x": {"net_return": 0.08, "capacity": 0.333}}},
            "predictive": {"passed": True, "domain_valid": True, "metrics": {"domain_valid": True, "sample_size": 1, "path_count": 1, "p_value_count": 1, "trials_tested": 2, "psr": 0.9, "dsr": 0.9, "pbo": 0.0, "data_snooping_probability": 0.1, "fdr_q_value": 0.1}},
        }
        assert not connection.execute(
            "SELECT analysis.research_check_complete(%s, 'combinatorial_paths')",
            [Jsonb({"passed": True, "domain_valid": True, "path_count": 1, "path_records": [{}]})],
        ).fetchone()[0]
        assert not connection.execute(
            "SELECT analysis.research_check_complete(%s, 'robustness')",
            [Jsonb({"passed": True, "domain_valid": True})],
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO analysis.experiment_manifest
               (experiment_family_id, expected_trial_count, expected_trial_keys, manifest_hash)
               VALUES (%s, 2, %s, %s)""",
            [family, Jsonb(["failed", "successful"]), content_hash(["failed", "successful"])],
        )
        failed_trial = connection.execute(
            """INSERT INTO analysis.research_trial
               (experiment_family_id, trial_key, input_cutoff, code_version, input_hash,
                available_at)
               VALUES (%s, 'failed', %s, 'test', %s, now())
               RETURNING id""",
            [family, cutoff, "3" * 64],
        ).fetchone()[0]
        successful_trial = connection.execute(
            """INSERT INTO analysis.research_trial
               (experiment_family_id, trial_key, input_cutoff, code_version, input_hash,
                available_at)
               VALUES (%s, 'successful', %s, 'test', %s, now())
               RETURNING id""",
            [family, cutoff, "9" * 64],
        ).fetchone()[0]
        for trial_id, result_hash in ((failed_trial, "3" * 64), (successful_trial, "9" * 64)):
            connection.execute(
                """INSERT INTO analysis.trial_universe_manifest
                   (research_trial_id, cutoff, expected_member_count, expected_members, manifest_hash)
                   VALUES (%s, %s, 1, %s, %s)""",
                    [trial_id, cutoff, Jsonb([str(instrument)]), content_hash([str(instrument)])],
            )
            connection.execute(
                """INSERT INTO analysis.universe_observation
                   (research_trial_id, instrument_id, cutoff, eligible, rank, observed_at, available_at, input_hash)
                   VALUES (%s, %s, %s, true, 1, now() - interval '1 day', now() - interval '1 day', %s)""",
                [trial_id, instrument, cutoff, result_hash],
            )
        availability = connection.execute(
            "SELECT min(observed_at) AS observed_at, min(available_at) AS available_at FROM analysis.universe_observation"
        ).fetchone()
        assert availability[0] > datetime.now(UTC) - timedelta(minutes=1)
        assert availability[1] > datetime.now(UTC) - timedelta(minutes=1)
        connection.execute(
            """INSERT INTO analysis.trial_result
               (research_trial_id, result_kind, observed_at, available_at, input_hash, metrics)
               VALUES (%s, 'negative-control', now(), now(), %s, %s)""",
            [failed_trial, "4" * 64, Jsonb({"passed": False})],
        )
        connection.execute("SAVEPOINT future_observation")
        with pytest.raises(psycopg.errors.RaiseException, match="future-dated"):
            connection.execute(
                """INSERT INTO analysis.trial_result
                   (research_trial_id, result_kind, observed_at, available_at, input_hash, metrics)
                   VALUES (%s, 'future-observation', now() + interval '1 day', now(), %s, %s)""",
                [failed_trial, "4" * 64, Jsonb({"passed": False})],
            )
        connection.execute("ROLLBACK TO SAVEPOINT future_observation")
        connection.execute(
            """INSERT INTO analysis.trial_result
               (research_trial_id, result_kind, observed_at, available_at, input_hash, metrics, outcome)
               VALUES (%s, 'validation', now(), now(), %s, %s, %s)""",
            [failed_trial, "3" * 64, Jsonb({"passed": False}), Jsonb({"passed": False, "checks": {}})],
        )
        connection.execute(
            """INSERT INTO analysis.trial_result
               (research_trial_id, result_kind, observed_at, available_at, input_hash, metrics, outcome)
                   VALUES (%s, 'validation', now(), now(), %s, %s, %s)""",
            [successful_trial, "9" * 64, Jsonb({"passed": True, **successful_checks}), Jsonb({"passed": True, "checks": successful_checks})],
        )
        connection.execute(
            "UPDATE analysis.research_trial SET status = 'failed', failure_reason = 'future information', finished_at = now() WHERE id = %s",
            [failed_trial],
        )
        connection.execute(
            "UPDATE analysis.research_trial SET status = 'succeeded', finished_at = now(), outcome = %s WHERE id = %s",
            [Jsonb({"edge": 0.1, "validation_result_present": True}), successful_trial],
        )
        revision = connection.execute(
            """INSERT INTO analysis.strategy_revision
               (strategy_key, revision, name, status, parameters, authority_group,
                hypothesis_id, experiment_family_id, artifact_id, artifact_hash)
               VALUES ('p1-strategy', 1, 'Phase 1', 'candidate', %s, 'p1-research', %s, %s, 'p1-artifact', %s)
               RETURNING id""",
            [Jsonb({"paper_only": True, "input_hash": "7" * 64}), hypothesis, family, "5" * 64],
        ).fetchone()[0]
        rejected_revision = connection.execute(
            """INSERT INTO analysis.strategy_revision
               (strategy_key, revision, name, status, parameters, authority_group,
                hypothesis_id, experiment_family_id)
               VALUES ('p1-rejected', 1, 'Rejected Phase 1', 'rejected', %s, 'p1-research', %s, %s)
               RETURNING id""",
            [Jsonb({"paper_only": True}), hypothesis, family],
        ).fetchone()[0]
        dossier = connection.execute(
            """INSERT INTO analysis.validation_dossier
               (strategy_revision_id, research_trial_id, sections, compiled_policy, artifact_id, artifact_hash)
               VALUES (%s, %s, %s, %s, 'p1-artifact', %s) RETURNING id""",
            [revision, successful_trial, Jsonb({"hypothesis": "incomplete"}), Jsonb({"paper_only": True}), "5" * 64],
        ).fetchone()[0]
        connection.execute("SAVEPOINT backdated_gate")
        with pytest.raises(psycopg.errors.RaiseException, match="database-owned"):
            connection.execute(
                """INSERT INTO analysis.validation_gate_result
                   (dossier_id, gate_code, verdict, evaluated_at, available_at)
                   VALUES (%s, 'pit_integrity', 'fail', now() - interval '1 day', now() - interval '1 day')""",
                [dossier],
            )
        connection.execute("ROLLBACK TO SAVEPOINT backdated_gate")
        connection.execute("SAVEPOINT incomplete_dossier")
        with pytest.raises(psycopg.errors.RaiseException, match="mandatory sections"):
            connection.execute("UPDATE analysis.validation_dossier SET status = 'sealed' WHERE id = %s", [dossier])
        connection.execute("ROLLBACK TO SAVEPOINT incomplete_dossier")

    with psycopg.connect(postgres_dsn) as connection:
        dossier = connection.execute(
            "SELECT id FROM analysis.validation_dossier WHERE strategy_revision_id = (SELECT id FROM analysis.strategy_revision WHERE strategy_key = 'p1-strategy')"
        ).fetchone()[0]
        revision = connection.execute(
            "SELECT id FROM analysis.strategy_revision WHERE strategy_key = 'p1-strategy'"
        ).fetchone()[0]
        sections = Jsonb({key: "complete" for key in ("hypothesis", "mechanism", "falsification", "controls", "validation", "economics", "lineage")})
        validation_result = connection.execute(
            "SELECT id FROM analysis.trial_result WHERE research_trial_id = %s AND result_kind = 'validation'",
            [successful_trial],
        ).fetchone()[0]
        evidence_payloads = {
            "controls": {"trial_input_hash": "9" * 64, "evaluator_id": "fixture.v1", "randomized_label_samples": [0.0], "white_noise_samples": [0.0]},
            "cpcv_paths": {"trial_input_hash": "9" * 64, "evaluator_id": "fixture.v1", "path_count": 1, "path_records": successful_checks["combinatorial_paths"]["path_records"]},
            "neutralization": {"trial_input_hash": "9" * 64, "evaluator_id": "fixture.v1", "samples": [0.1]},
            "parameter_stability": {"trial_input_hash": "9" * 64, "evaluator_id": "fixture.v1", "samples": [0.1, 0.1, 0.1]},
            "mechanism_falsification": {"trial_input_hash": "9" * 64, "evaluator_id": "fixture.v1", "samples": [0.1]},
            "multiple_testing": {"trial_input_hash": "9" * 64, "evaluator_id": "fixture.v1", "path_returns": [0.1], "p_values": [0.1], "metrics": successful_checks["multiple_testing"]},
        }
        evaluator_run = connection.execute(
            """INSERT INTO analysis.run
               (run_type, input_cutoff, code_version, input_hash, inputs,
                started_at, finished_at, status, summary)
               VALUES ('research_evaluator', %s, 'fixture-code.v1', %s, %s,
                       clock_timestamp(), clock_timestamp(), 'succeeded', %s)
               RETURNING id""",
            [cutoff, "9" * 64, Jsonb({"fixture": True}), Jsonb({"fixture": True})],
        ).fetchone()[0]
        signing_key = "phase1-test-signing-key"
        source_rows = {}
        for kind, payload in evidence_payloads.items():
            payload = {
                **payload,
                "input_hash": "9" * 64,
                "universe_hash": content_hash([str(instrument)]),
                "feature_hash": "b" * 64,
                "evidence_kind": kind,
                "evaluator_code_version": "fixture-code.v1",
            }
            sample_count = 2 if kind == "controls" else 3 if kind == "parameter_stability" else 1
            available_at = connection.execute("SELECT clock_timestamp() AS now").fetchone()[0]
            output_hash = connection.execute(
                """SELECT analysis.research_evaluator_output_hash_v2(
                           %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s)""",
                [successful_trial, validation_result, evaluator_run, kind, "fixture.v1", "fixture-code.v1",
                 "9" * 64, content_hash([str(instrument)]), "b" * 64, sample_count, Jsonb(payload), available_at],
            ).fetchone()[0]
            signature = connection.execute(
                """SELECT encode(hmac(analysis.research_evaluator_signature_payload(
                           %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s), %s, 'sha256'), 'hex')""",
                [successful_trial, validation_result, evaluator_run, kind, "fixture.v1", "fixture-code.v1",
                 "9" * 64, content_hash([str(instrument)]), "b" * 64, sample_count, output_hash, available_at, signing_key],
            ).fetchone()[0]
            source_rows[kind] = connection.execute(
                """INSERT INTO analysis.research_evaluator_output
                   (research_trial_id, trial_result_id, analysis_run_id, evidence_kind,
                    evaluator_id, evaluator_code_version, input_hash, universe_hash,
                    feature_hash, sample_count, domain_valid, raw_output, output_hash, signature, available_at)
                   VALUES (%s, %s, %s, %s, 'fixture.v1', 'fixture-code.v1', %s, %s,
                           %s, %s, true, %s, %s, %s, %s)
                   RETURNING id, output_hash, raw_output""",
                [successful_trial, validation_result, evaluator_run, kind, "9" * 64,
                     content_hash([str(instrument)]), "b" * 64, sample_count, Jsonb(payload), output_hash, signature, available_at],
            ).fetchone()
        connection.execute("SAVEPOINT forged_evaluator_output")
        attacker_available = connection.execute("SELECT clock_timestamp() AS now").fetchone()[0]
        attacker_hash = connection.execute(
            """SELECT analysis.research_evaluator_output_hash_v2(
                       %s, %s, %s, 'controls', 'fixture.v1', 'fixture-code.v1',
                       %s, %s, %s, 2, true, %s, %s)""",
            [successful_trial, validation_result, evaluator_run, "9" * 64,
             content_hash([str(instrument)]), "b" * 64, Jsonb(source_rows["controls"][2]), attacker_available],
        ).fetchone()[0]
        attacker_signature = connection.execute(
            """SELECT encode(hmac(convert_to(analysis.research_evaluator_signature_payload(
                       %s, %s, %s, 'controls', 'fixture.v1', 'fixture-code.v1',
                       %s, %s, %s, 2, true, %s, %s), 'UTF8'),
                       convert_to('attacker-key', 'UTF8'), 'sha256'::TEXT), 'hex')""",
            [successful_trial, validation_result, evaluator_run, "9" * 64,
             content_hash([str(instrument)]), "b" * 64, attacker_hash, attacker_available],
        ).fetchone()[0]
        connection.execute("SELECT set_config('app.research_evaluator_signing_key', 'attacker-key', true)")
        with pytest.raises(psycopg.errors.RaiseException, match="signature or content hash"):
            connection.execute(
                """INSERT INTO analysis.research_evaluator_output
                   (research_trial_id, trial_result_id, analysis_run_id, evidence_kind,
                    evaluator_id, evaluator_code_version, input_hash, universe_hash,
                    feature_hash, sample_count, domain_valid, raw_output, output_hash, signature, available_at)
                   VALUES (%s, %s, %s, 'controls', 'fixture.v1', 'fixture-code.v1', %s, %s,
                           %s, 2, true, %s, %s, %s, now())""",
                [successful_trial, validation_result, evaluator_run, "9" * 64,
                 content_hash([str(instrument)]), "b" * 64, Jsonb(source_rows["controls"][2]), attacker_hash, attacker_signature],
            )
        connection.execute("ROLLBACK TO SAVEPOINT forged_evaluator_output")
        for kind, payload in evidence_payloads.items():
            connection.execute(
                """INSERT INTO analysis.research_evidence_manifest
                   (research_trial_id, trial_result_id, evidence_kind, evaluator_id,
                    evaluator_code_version, input_hash, universe_hash, feature_hash,
                    evaluator_output_id, sample_count, domain_valid, payload, evidence_hash)
                   VALUES (%s, %s, %s, 'fixture.v1', 'fixture-code.v1', %s, %s, %s,
                           %s, %s, true, %s, %s)""",
                [successful_trial, validation_result, kind, "9" * 64,
                 content_hash([str(instrument)]), "b" * 64, source_rows[kind][0],
                 2 if kind == "controls" else 3 if kind == "parameter_stability" else 1,
                 Jsonb(source_rows[kind][2]), source_rows[kind][1]],
            )
        numeric_formats = connection.execute(
            "SELECT analysis.canonical_forecast_number(1.0), analysis.canonical_forecast_number(0.0), analysis.canonical_forecast_number(-0.0), analysis.canonical_forecast_number(0.1000)"
        ).fetchone()
        assert tuple(numeric_formats) == ("1", "0", "0", "0.1")
        connection.execute("SAVEPOINT fabricated_evidence")
        with pytest.raises(psycopg.errors.RaiseException, match="immutable evaluator output|linked non-empty|incomplete"):
            connection.execute(
                """INSERT INTO analysis.research_evidence_manifest
                   (research_trial_id, trial_result_id, evidence_kind, evaluator_id, sample_count, domain_valid, payload)
                   VALUES (%s, %s, 'controls', 'fixture.v1', 2, true, %s)""",
                [successful_trial, validation_result, Jsonb({"trial_input_hash": "9" * 64, "evaluator_id": "fixture.v1", "randomized_label_samples": [0.9], "white_noise_samples": [0.0]})],
            )
        connection.execute("ROLLBACK TO SAVEPOINT fabricated_evidence")
        gate_check_names = {
            "pit_integrity": ["pit"],
            "denominator_completeness": ["denominator", "attempt_manifest"],
            "oos_predictive_validity": ["predictive", "multiple_testing"],
            "falsification_and_robustness": ["mechanism", "negative_controls", "parameter_stability", "neutralization", "combinatorial_paths", "robustness"],
            "economic_promotability": ["cost_capacity", "neutralization"],
        }
        evidence_hashes = [row[0] for row in connection.execute(
            "SELECT evidence_hash FROM analysis.research_evidence_manifest WHERE trial_result_id = %s ORDER BY evidence_kind",
            [validation_result],
        ).fetchall()]
        connection.execute("SAVEPOINT forged_gate_evidence")
        for gate in ("pit_integrity", "denominator_completeness", "oos_predictive_validity", "falsification_and_robustness", "economic_promotability"):
            connection.execute(
                "INSERT INTO analysis.validation_gate_result (dossier_id, gate_code, verdict, metrics, evidence) VALUES (%s, %s, 'pass', %s, %s)",
                [dossier, gate, Jsonb({"passed": True}), Jsonb({"trial_result_id": "forged-result"})],
            )
        with pytest.raises(psycopg.errors.RaiseException, match="all five passing gates|evidence-backed"):
            connection.execute("UPDATE analysis.validation_dossier SET sections = %s, status = 'sealed' WHERE id = %s", [sections, dossier])
        connection.execute("ROLLBACK TO SAVEPOINT forged_gate_evidence")
        for gate in ("pit_integrity", "denominator_completeness", "oos_predictive_validity", "falsification_and_robustness", "economic_promotability"):
            connection.execute(
                "INSERT INTO analysis.validation_gate_result (dossier_id, gate_code, verdict, metrics, evidence) VALUES (%s, %s, 'pass', %s, %s)",
                    [dossier, gate, Jsonb({"passed": True, "domain_valid": True, "validation_result_id": str(validation_result), "validation_result_input_hash": "9" * 64, "evidence_manifest_hashes": evidence_hashes, "checks": {name: successful_checks[name] for name in gate_check_names[gate]}}), Jsonb({"trial_result_id": str(validation_result), "input_hash": "9" * 64, "checks": gate_check_names[gate]})],
            )
        connection.execute("UPDATE analysis.validation_dossier SET sections = %s, status = 'sealed' WHERE id = %s", [sections, dossier])
        evaluation = connection.execute(
            """INSERT INTO analysis.strategy_evaluation
                   (strategy_revision_id, validation_dossier_id, research_trial_id, artifact_id, artifact_hash, input_hash,
                    evaluation_type, evaluated_at, verdict, metrics, evidence)
               VALUES (%s, %s, %s, 'p1-artifact', %s, %s, 'out_of_sample', now(), 'pass', %s, %s) RETURNING id""",
            [revision, dossier, successful_trial, "5" * 64, "7" * 64, Jsonb({"artifact_hash": "5" * 64, "input_hash": "7" * 64, "target": "return", "forecasts": [{"horizon": "1d", "forecast_value": 0.1, "forecast_distribution": {"positive_return_after_costs": 0.1}, "probability_semantics": None}]}), Jsonb({"paper_only": True})],
        ).fetchone()[0]
        forecast_time = datetime.now(UTC)
        forecast = build_strategy_forecast(
            ticker="P1T", opportunity_episode_id="episode:p1", strategy_revision_id=revision,
            strategy_evaluation_id=str(evaluation), target="return", horizon="1d", forecast_value=0.1,
            forecast_distribution={"positive_return_after_costs": 0.1},
            model_artifact_id="p1-artifact", artifact_hash="5" * 64, input_hash="7" * 64,
            as_of=cutoff, generated_at=forecast_time, available_at=forecast_time,
        )
        connection.execute(
            """INSERT INTO analysis.strategy_forecast
            (id, strategy_revision_id, strategy_evaluation_id, instrument_id, opportunity_episode_id, target, horizon,
                    forecast_value, forecast_distribution, model_artifact_id, artifact_hash, input_hash, as_of, input_cutoff,
                generated_at, available_at)
               VALUES (%s, %s, %s, (SELECT id FROM catalog.instrument WHERE symbol = 'P1T'),
                           'episode:p1', 'return', '1d', 0.1, %s, 'p1-artifact', %s, %s,
                       %s, %s, %s, %s)""",
            [forecast.strategy_forecast_id, revision, evaluation, Jsonb({"positive_return_after_costs": 0.1}), "5" * 64, "7" * 64, cutoff, cutoff, forecast_time, forecast_time],
        )
        connection.execute("SAVEPOINT backdated_forecast")
        with pytest.raises(psycopg.errors.RaiseException, match="actual availability"):
            connection.execute(
                """INSERT INTO analysis.strategy_forecast
                   (id, strategy_revision_id, strategy_evaluation_id, instrument_id, opportunity_episode_id,
                    target, horizon, forecast_value, forecast_distribution, model_artifact_id, artifact_hash,
                    input_hash, as_of, input_cutoff, generated_at, available_at)
                   VALUES ('forecast:strategy-forecast:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', %s, %s,
                           (SELECT id FROM catalog.instrument WHERE symbol = 'P1T'), 'episode:p1', 'return',
                           '1d-backdated', 0.1, %s, 'p1-artifact', %s, %s, %s, %s,
                           now() - interval '1 day', now() - interval '1 day')""",
                [revision, evaluation, Jsonb({"positive_return_after_costs": 0.1}), "5" * 64, "7" * 64, cutoff, cutoff],
            )
        connection.execute("ROLLBACK TO SAVEPOINT backdated_forecast")
        connection.execute("SAVEPOINT midnight_forecast")
        with pytest.raises(psycopg.errors.RaiseException, match="actual availability"):
            connection.execute(
                """INSERT INTO analysis.strategy_forecast
                   (id, strategy_revision_id, strategy_evaluation_id, instrument_id, opportunity_episode_id,
                    target, horizon, forecast_value, forecast_distribution, model_artifact_id, artifact_hash,
                    input_hash, as_of, input_cutoff, generated_at, available_at)
                   VALUES (%s, %s, %s, (SELECT id FROM catalog.instrument WHERE symbol = 'P1T'), 'episode:p1',
                           'return', '1d-midnight', 0.1, %s, 'p1-artifact', %s, %s, %s, %s,
                           date_trunc('day', now()), date_trunc('day', now()))""",
                [forecast.strategy_forecast_id, revision, evaluation, Jsonb({"positive_return_after_costs": 0.1}), "5" * 64, "7" * 64, cutoff, cutoff],
            )
        connection.execute("ROLLBACK TO SAVEPOINT midnight_forecast")
        connection.execute("UPDATE analysis.strategy_revision SET status = 'active' WHERE id = %s", [revision])
        assert connection.execute("SELECT status FROM analysis.validation_dossier WHERE id = %s", [dossier]).fetchone()[0] == "sealed"
        assert connection.execute("SELECT status FROM analysis.strategy_revision WHERE id = %s", [revision]).fetchone()[0] == "active"
        assert connection.execute("SELECT status FROM analysis.strategy_revision WHERE id = %s", [rejected_revision]).fetchone()[0] == "rejected"
        assert connection.execute("SELECT status FROM analysis.research_trial WHERE id = %s", [successful_trial]).fetchone()[0] == "succeeded"
        assert connection.execute("SELECT id FROM analysis.strategy_forecast WHERE id = %s", [forecast.strategy_forecast_id]).fetchone()[0] == forecast.strategy_forecast_id
        assert connection.execute("SELECT count(*) FROM analysis.universe_observation").fetchone()[0] == 2


def test_terminal_trial_insert_requires_manifest_and_validation_result(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        hypothesis = connection.execute(
            """INSERT INTO analysis.hypothesis
               (hypothesis_key, statement, mechanism_class, falsification, input_hash)
               VALUES ('terminal-hypothesis', 'test', 'quality', 'negative', %s) RETURNING id""",
            ["1" * 64],
        ).fetchone()[0]
        family = connection.execute(
            """INSERT INTO analysis.experiment_family
               (hypothesis_id, family_key, name, input_hash)
               VALUES (%s, 'terminal-family', 'Terminal test', %s) RETURNING id""",
            [hypothesis, "2" * 64],
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.RaiseException, match="validation result and complete universe"):
            connection.execute(
                """INSERT INTO analysis.research_trial
                   (experiment_family_id, trial_key, input_cutoff, code_version, input_hash, status)
                   VALUES (%s, 'terminal-without-evidence', now(), 'test', %s, 'succeeded')""",
                [family, "3" * 64],
            )
