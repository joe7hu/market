from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from investment_panel.database.migrations import upgrade_database


def test_phase1_trial_dossier_forecast_and_universe_authority(postgres_dsn: str) -> None:
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
        failed_trial = connection.execute(
            """INSERT INTO analysis.research_trial
               (experiment_family_id, trial_key, input_cutoff, code_version, input_hash,
                status, failure_reason, available_at)
               VALUES (%s, 'failed', %s, 'test', %s, 'failed', 'future information', now())
               RETURNING id""",
            [family, cutoff, "3" * 64],
        ).fetchone()[0]
        successful_trial = connection.execute(
            """INSERT INTO analysis.research_trial
               (experiment_family_id, trial_key, input_cutoff, code_version, input_hash,
                status, finished_at, outcome, available_at)
               VALUES (%s, 'successful', %s, 'test', %s, 'succeeded', now(), %s, now())
               RETURNING id""",
            [family, cutoff, "9" * 64, Jsonb({"edge": 0.1})],
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO analysis.trial_result
               (research_trial_id, result_kind, observed_at, available_at, input_hash, metrics)
               VALUES (%s, 'negative-control', now(), now(), %s, %s)""",
            [failed_trial, "4" * 64, Jsonb({"passed": False})],
        )
        revision = connection.execute(
            """INSERT INTO analysis.strategy_revision
               (strategy_key, revision, name, status, parameters, authority_group,
                hypothesis_id, experiment_family_id, artifact_id, artifact_hash)
               VALUES ('p1-strategy', 1, 'Phase 1', 'candidate', %s, 'p1-research', %s, %s, 'p1-artifact', %s)
               RETURNING id""",
            [Jsonb({"paper_only": True}), hypothesis, family, "5" * 64],
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
            "INSERT INTO analysis.validation_dossier (strategy_revision_id, sections) VALUES (%s, %s) RETURNING id",
            [revision, Jsonb({"hypothesis": "incomplete"})],
        ).fetchone()[0]
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
        for gate in ("pit_integrity", "denominator_completeness", "oos_predictive_validity", "falsification_and_robustness", "economic_promotability"):
            connection.execute(
                "INSERT INTO analysis.validation_gate_result (dossier_id, gate_code, verdict, available_at) VALUES (%s, %s, 'pass', now())",
                [dossier, gate],
            )
        connection.execute("UPDATE analysis.validation_dossier SET sections = %s, status = 'sealed' WHERE id = %s", [sections, dossier])
        connection.execute("UPDATE analysis.strategy_revision SET status = 'active' WHERE id = %s", [revision])
        forecast_id = "forecast:p1-test"
        connection.execute(
            """INSERT INTO analysis.strategy_forecast
               (id, strategy_revision_id, instrument_id, opportunity_episode_id, target, horizon,
                forecast_value, model_artifact_id, artifact_hash, input_hash, as_of, input_cutoff,
                generated_at, available_at)
               VALUES (%s, %s, (SELECT id FROM catalog.instrument WHERE symbol = 'P1T'),
                       'episode:p1', 'return', '1d', 0.1, 'p1-artifact', %s, %s,
                       now(), now(), now() - interval '1 hour', now())""",
            [forecast_id, revision, "6" * 64, "7" * 64],
        )
        connection.execute(
            """INSERT INTO analysis.universe_observation
               (research_trial_id, instrument_id, cutoff, eligible, rank, observed_at, available_at, input_hash)
               VALUES ((SELECT id FROM analysis.research_trial WHERE trial_key = 'failed'), %s, %s, true, 1, now(), now(), %s)""",
            [instrument, cutoff, "8" * 64],
        )
        assert connection.execute("SELECT status FROM analysis.validation_dossier WHERE id = %s", [dossier]).fetchone()[0] == "sealed"
        assert connection.execute("SELECT status FROM analysis.strategy_revision WHERE id = %s", [revision]).fetchone()[0] == "active"
        assert connection.execute("SELECT status FROM analysis.strategy_revision WHERE id = %s", [rejected_revision]).fetchone()[0] == "rejected"
        assert connection.execute("SELECT status FROM analysis.research_trial WHERE id = %s", [successful_trial]).fetchone()[0] == "succeeded"
        assert connection.execute("SELECT id FROM analysis.strategy_forecast WHERE id = %s", [forecast_id]).fetchone()[0] == forecast_id
        assert connection.execute("SELECT count(*) FROM analysis.universe_observation").fetchone()[0] == 1
