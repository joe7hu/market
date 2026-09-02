from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from investment_panel.analysis.stock_alpha import content_hash


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
        connection.execute(
            """INSERT INTO analysis.strategy_monitoring_evidence
               (strategy_revision_id, evidence_kind, input_cutoff, observed_at, available_at,
                input_hash, metrics, evidence)
               VALUES (%s, 'decay', %s, %s, %s, %s, %s, %s)""",
            [revision, now, now, now, "b" * 64, Jsonb({"decay": 0.1}), Jsonb({"paper_only": True})],
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
        with pytest.raises(psycopg.errors.RaiseException, match="permanent non-promotable"):
            connection.execute("UPDATE analysis.strategy_revision SET status = 'active' WHERE id = %s", [martingale])
        connection.execute("ROLLBACK TO SAVEPOINT martingale_promotion")


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
