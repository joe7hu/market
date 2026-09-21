"""Single-snapshot creation, data preservation, and runtime permissions."""

import hashlib
import json
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb
from sqlalchemy.exc import ProgrammingError

from investment_panel.infrastructure.postgres.migrations import HEAD_REVISION, alembic_config, upgrade_database
from investment_panel.infrastructure.postgres.panel_models import QUERY_POLICIES
from migrations.baseline_contract import BASELINE_REVISION, BASELINE_SCHEMA_HASHES
from migrations.schema_contract import schema_contract


@pytest.fixture
def baseline_postgres_dsn(postgres_dsn):
    upgrade_database(postgres_dsn, BASELINE_REVISION)
    return postgres_dsn


def catalog_hash(connection):
    return hashlib.sha256(json.dumps(schema_contract(connection), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_baseline_matches_verified_schema_and_application_queries(baseline_postgres_dsn):
    with psycopg.connect(baseline_postgres_dsn) as connection:
        assert catalog_hash(connection) in BASELINE_SCHEMA_HASHES
        connection.execute("SET LOCAL ROLE market_app")
        connection.execute("SELECT version_num FROM public.alembic_version").fetchone()
        for policy in QUERY_POLICIES.values():
            if not policy.custom_loader:
                connection.execute("EXPLAIN " + policy.query).fetchall()
        assert connection.execute("SELECT to_regclass('app.review_page_snapshot')").fetchone()[0] is None
        for relation in ('app.trade_journal','app.alert','analysis.event_decision_packet','analysis.event_scout_event','app.decision_truth'):
            assert connection.execute("SELECT has_table_privilege(current_user,%s,'INSERT')", [relation]).fetchone()[0]
        assert not connection.execute("SELECT has_table_privilege(current_user,'analysis.research_evaluator_signing_secret','SELECT')").fetchone()[0]


def test_reapplying_snapshot_preserves_user_records_and_secret(baseline_postgres_dsn):
    with psycopg.connect(baseline_postgres_dsn) as connection:
        connection.execute("UPDATE catalog.instrument SET name='Keep my name' WHERE symbol='QQQ'")
        before = connection.execute("SELECT secret FROM analysis.phase4_allocation_signing_secret").fetchone()[0]
    upgrade_database(baseline_postgres_dsn, BASELINE_REVISION)
    with psycopg.connect(baseline_postgres_dsn) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == BASELINE_REVISION
        assert connection.execute("SELECT name FROM catalog.instrument WHERE symbol='QQQ'").fetchone()[0] == 'Keep my name'
        assert connection.execute("SELECT secret FROM analysis.phase4_allocation_signing_secret").fetchone()[0] == before


def test_archived_revision_fails_closed_without_changing_data(postgres_dsn):
    upgrade_database(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("UPDATE catalog.instrument SET name='Keep my name' WHERE symbol='QQQ'")
        connection.execute("UPDATE public.alembic_version SET version_num='20260907_0005'")
    with pytest.raises(RuntimeError, match='archived migration revision'):
        upgrade_database(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == '20260907_0005'
        assert connection.execute("SELECT name FROM catalog.instrument WHERE symbol='QQQ'").fetchone()[0] == 'Keep my name'


def test_migration_lock_prevents_concurrent_runner_and_is_released(postgres_dsn):
    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(hashtextextended('market-schema-migration',0))")
        with pytest.raises(RuntimeError, match='another Market schema migration'):
            upgrade_database(postgres_dsn)
    upgrade_database(postgres_dsn)


def test_encoded_password_survives_alembic_config():
    dsn = 'postgresql://user:example%40password%25@localhost/market'
    assert alembic_config(dsn).get_main_option('sqlalchemy.url') == dsn.replace('postgresql://','postgresql+psycopg://')


def test_migrations_directory_has_snapshot_and_forward_schema():
    root = Path(__file__).resolve().parents[2]
    versions = sorted((root / 'migrations' / 'versions').glob('*.py'))
    assert [path.name for path in versions] == [
        '20260907_0006_baseline.py',
        '20260908_0007_continuous_advisor.py',
        '20260909_0008_backfill_publication_superseded_at.py',
        '20260909_0009_strategy_implementation_identity.py',
        '20260909_0010_strategy_definition_policy.py',
        '20260910_0011_market_app_option_job_privileges.py',
        '20260910_0012_market_app_option_capture_pipeline.py',
        '20260910_0013_market_app_option_partition_maintenance.py',
        '20260910_0014_market_app_option_partition_access.py',
        '20260910_0015_market_app_option_partition_authorization.py',
        '20260910_0016_market_app_option_partition_safety.py',
        '20260910_0017_market_app_option_partition_locking.py',
        '20260911_0018_strategy_run_provenance.py',
        '20260911_0019_advisor_resolution_attempts.py',
        '20260912_0020_workbench_indexes_promotion.py',
        '20260912_0021_paper_book_scope.py',
        '20260912_0022_paper_trade_projection.py',
        '20260918_0023_read_model_lookup_indexes.py',
        '20260919_0024_paper_account.py',
        '20260919_0025_paper_nav.py',
        '20260920_0026_options_radar_binding_repair.py',
        '20260920_0027_options_radar_orphan_candidate_repair.py',
        '20260920_0028_market_app_setting_privileges.py',
    ]
    sql_files = sorted((root / 'migrations' / 'baseline').glob('*.sql'))
    assert len(sql_files) == 27
    assert max(path.read_text().count('\n') for path in sql_files) < 1500
    sql = '\n'.join(path.read_text() for path in sql_files)
    assert 'ADD COLUMN' not in sql
    assert 'DROP COLUMN' not in sql
    assert 'ALTER COLUMN' not in sql
    assert 'ALTER INDEX' not in sql


def test_known_forward_revision_upgrades_to_head(postgres_dsn):
    upgrade_database(postgres_dsn, '20260908_0007')
    upgrade_database(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD_REVISION


def test_setting_privilege_migration_restores_runtime_write_access(postgres_dsn):
    upgrade_database(postgres_dsn, '20260920_0027')
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute('REVOKE INSERT,UPDATE ON TABLE app.setting FROM market_app')
        connection.commit()
    upgrade_database(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute('SET LOCAL ROLE market_app')
        assert connection.execute("SELECT has_table_privilege(current_user, 'app.setting', 'INSERT')").fetchone()[0]
        assert connection.execute("SELECT has_table_privilege(current_user, 'app.setting', 'UPDATE')").fetchone()[0]


def test_previous_strategy_binding_revision_upgrades_to_head(postgres_dsn):
    upgrade_database(postgres_dsn, '20260909_0009')
    upgrade_database(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD_REVISION
        connection.execute("SET LOCAL ROLE market_app")
        for relation, privileges in {
            'catalog.option_contract': ('INSERT', 'UPDATE'),
            'analysis.option_recovery_program_session': ('INSERT', 'UPDATE'),
            'analysis.option_surface_summary': ('INSERT',),
            'analysis.option_relative_value': ('INSERT',),
            'analysis.option_surface_shift': ('INSERT',),
            'analysis.option_relative_value_verification': ('INSERT',),
            'raw.option_capture_generation': ('INSERT', 'UPDATE'),
            'raw.option_quote': ('INSERT', 'UPDATE', 'DELETE'),
            'raw.option_snapshot': ('INSERT', 'UPDATE', 'DELETE'),
            'ops.option_quote_partition_policy': ('SELECT',),
        }.items():
            for privilege in privileges:
                assert connection.execute(
                    "SELECT has_table_privilege(current_user,%s,%s)", [relation, privilege]
                ).fetchone()[0]
        for sequence in (
            'analysis.option_relative_value_id_seq',
            'analysis.option_relative_value_verification_id_seq',
            'analysis.option_surface_summary_id_seq',
            'raw.option_capture_generation_id_seq',
            'raw.option_quote_id_seq',
            'raw.option_snapshot_id_seq',
        ):
            assert connection.execute(
                "SELECT has_sequence_privilege(current_user,%s,'USAGE')", [sequence]
                ).fetchone()[0]
        connection.execute("SELECT count(*) FROM ops.option_quote_partition_policy").fetchone()
        assert connection.execute(
            "SELECT has_function_privilege(current_user,%s,'EXECUTE')",
            ['raw.detach_option_quote_partition(text,boolean)'],
        ).fetchone()[0]
        connection.execute(
            "SELECT raw.ensure_option_quote_partition(%s, %s, %s)",
            ['option_quote_20260910', '2026-09-10', '2026-09-11'],
        )
        connection.rollback()
        connection.execute("CREATE TABLE raw.option_quote_20990103 (id bigint)")
        connection.execute("SET LOCAL ROLE market_app")
        with pytest.raises(psycopg.Error, match='relation exists'):
            connection.execute(
                "SELECT raw.ensure_option_quote_partition(%s, %s, %s)",
                ['option_quote_20990103', '2099-01-03', '2099-01-04'],
            )
        connection.rollback()
        connection.execute("SET LOCAL ROLE market_app")
        with pytest.raises(psycopg.Error, match='first day'):
            connection.execute(
                "SELECT raw.ensure_option_quote_partition(%s, %s, %s)",
                ['option_quote_209901', '2099-01-10', '2099-02-01'],
            )
        connection.rollback()
        connection.execute("SET LOCAL ROLE market_app")
        connection.execute(
            "SELECT raw.ensure_option_quote_partition(%s, %s, %s)",
            ['option_quote_20990102', '2099-01-02', '2099-01-03'],
        )
        with pytest.raises(psycopg.Error, match='required'):
            connection.execute(
                "SELECT raw.detach_option_quote_partition(%s, %s)",
                ['option_quote_20990102', None],
            )
        connection.rollback()
        connection.execute("SET LOCAL ROLE market_app")
        connection.execute(
            "SELECT raw.ensure_option_quote_partition(%s, %s, %s)",
            ['option_quote_20990102', '2099-01-02', '2099-01-03'],
        )
        with pytest.raises(psycopg.Error, match='privileged maintenance'):
            connection.execute(
                "SELECT raw.detach_option_quote_partition(%s, false)",
                ['option_quote_20990102'],
            )
        connection.rollback()
        connection.execute("SET LOCAL ROLE market_app")
        connection.execute(
            "SELECT raw.ensure_option_quote_partition(%s, %s, %s)",
            ['option_quote_20990101', '2099-01-01', '2099-01-02'],
        )
        assert connection.execute(
            "SELECT has_table_privilege(current_user,%s,'SELECT')",
            ['raw.option_quote_20990101'],
        ).fetchone()[0]
        assert connection.execute(
            "SELECT raw.detach_option_quote_partition(%s, true)",
            ['option_quote_20990101'],
        ).fetchone()[0]
        connection.rollback()


def test_options_radar_binding_repair_migration_replaces_legacy_identity(postgres_dsn):
    upgrade_database(postgres_dsn, '20260919_0025')
    with psycopg.connect(postgres_dsn) as connection:
        legacy = connection.execute(
            """INSERT INTO analysis.strategy_revision
               (strategy_key, revision, name, status, parameters, authority_group,
                implementation_id, implementation_version, promoted_at)
               VALUES ('options-radar-core', 3, 'Professional options radar', 'active', %s,
                       'options-radar-core', 'options_radar', 'option-professional-v3-ticket', now())
               RETURNING id""",
            [Jsonb({'feature_version': 'option-professional-v2', 'contract_version': 3})],
        ).fetchone()[0]
        candidate = connection.execute(
            """INSERT INTO analysis.strategy_revision
               (strategy_key, revision, name, status, parameters, supersedes_id, authority_group,
                implementation_id, implementation_version)
               VALUES ('options-radar-core__agent_legacy', 1, 'Legacy candidate', 'candidate', %s,
                       %s, 'options-radar-core', 'options_radar', 'option-professional-v3-ticket')
               RETURNING id""",
            [Jsonb({'feature_version': 'option-professional-v2', 'contract_version': 3}), legacy],
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO analysis.strategy_revision
               (strategy_key, revision, name, status, parameters, authority_group,
                implementation_id, implementation_version)
               VALUES ('options-radar-core', 4, 'Interrupted successor', 'candidate', %s,
                       'options-radar-core', 'options_radar', 'option-professional-v3-ticket')""",
            [Jsonb({'feature_version': 'option-professional-v3-ticket', 'contract_version': 3})],
        )
        run = connection.execute(
            """INSERT INTO analysis.run
               (run_type, input_cutoff, code_version, feature_versions, strategy_revision_id,
                input_hash, started_at, finished_at, status)
               VALUES ('options_radar', now(), 'legacy-binding-test', '{}', %s, %s,
                       now(), now(), 'succeeded')
               RETURNING id""",
            [legacy, '0' * 64],
        ).fetchone()[0]
        publication = connection.execute(
            """INSERT INTO app.publication (scope, analysis_run_id, status, published_at)
               VALUES ('options-radar', %s, 'published', now())
               RETURNING id""",
            [run],
        ).fetchone()[0]
        content_hash = 'a' * 64
        connection.execute(
            "INSERT INTO app.publication_payload (content_hash, payload) VALUES (%s, '{}')",
            [content_hash],
        )
        connection.execute(
            """INSERT INTO app.current_publication_item
               (scope, publication_id, model_name, stable_key, rank, content_hash)
               VALUES ('options-radar', %s, 'option_radar_summary', 'legacy', 1, %s)""",
            [publication, content_hash],
        )
        incumbent_publication = connection.execute(
            """INSERT INTO app.publication (scope, analysis_run_id, status, published_at)
               VALUES (concat('options-paper-incumbent:', %s), %s, 'published', now())
               RETURNING id""",
            [legacy, run],
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO app.current_publication_item
               (scope, publication_id, model_name, stable_key, rank, content_hash)
               VALUES (concat('options-paper-incumbent:', %s), %s,
                       'option_paper_experiment', 'legacy', 1, %s)""",
            [legacy, incumbent_publication, content_hash],
        )
        instrument = connection.execute(
            "INSERT INTO catalog.instrument (symbol, asset_class) VALUES ('LGCY', 'equity') RETURNING id",
        ).fetchone()[0]
        decision = connection.execute(
            """INSERT INTO analysis.decision
               (run_id, decision_key, kind, instrument_id, as_of, state, input_hash,
                strategy_revision_id, sample_eligible)
               VALUES (%s, 'legacy-shadow', 'option', %s, now(), 'READY', %s, %s, true)
               RETURNING id""",
            [run, instrument, 'd' * 64, legacy],
        ).fetchone()[0]
        shadow = connection.execute(
            """INSERT INTO analysis.shadow_trade
               (decision_id, status, source_kind, pending_entry_reason, metrics)
               VALUES (%s, 'pending', 'options_paper_experiment', 'later_quote_required', %s)
               RETURNING id""",
            [decision, Jsonb({'publication_id': str(incumbent_publication)})],
        ).fetchone()[0]
        candidate_run = connection.execute(
            """INSERT INTO analysis.run
               (run_type, input_cutoff, code_version, feature_versions, strategy_revision_id,
                input_hash, started_at, finished_at, status)
               VALUES ('options-paper-experiment', now(), 'legacy-candidate-test', '{}', %s, %s,
                       now(), now(), 'succeeded')
               RETURNING id""",
            [candidate, 'b' * 64],
        ).fetchone()[0]
        candidate_publication = connection.execute(
            """INSERT INTO app.publication (scope, analysis_run_id, status, published_at)
               VALUES (concat('options-paper-experiment:', %s), %s, 'published', now())
               RETURNING id""",
            [candidate, candidate_run],
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO app.current_publication_item
               (scope, publication_id, model_name, stable_key, rank, content_hash)
               VALUES (concat('options-paper-experiment:', %s), %s,
                       'option_paper_experiment', 'legacy', 1, %s)""",
            [candidate, candidate_publication, content_hash],
        )
        candidate_decision = connection.execute(
            """INSERT INTO analysis.decision
               (run_id, decision_key, kind, instrument_id, as_of, state, input_hash,
                strategy_revision_id, sample_eligible)
               VALUES (%s, 'candidate-shadow', 'option', %s, now(), 'READY', %s, %s, true)
               RETURNING id""",
            [candidate_run, instrument, 'e' * 64, candidate],
        ).fetchone()[0]
        candidate_shadow = connection.execute(
            """INSERT INTO analysis.shadow_trade
               (decision_id, status, source_kind, pending_entry_reason, metrics)
               VALUES (%s, 'pending', 'options_paper_experiment', 'later_quote_required', %s)
               RETURNING id""",
            [candidate_decision, Jsonb({'publication_id': str(candidate_publication)})],
        ).fetchone()[0]

    upgrade_database(postgres_dsn)

    with psycopg.connect(postgres_dsn) as connection:
        rows = connection.execute(
            """SELECT strategy_key, revision, status, implementation_id, implementation_version,
                      parameters->>'feature_version' AS feature_version
                 FROM analysis.strategy_revision
                WHERE authority_group = 'options-radar-core'
                ORDER BY revision, strategy_key""",
        ).fetchall()
        retired_publication = connection.execute(
            "SELECT status, superseded_at IS NOT NULL FROM app.publication WHERE id = %s",
            [publication],
        ).fetchone()
        retired_incumbent = connection.execute(
            "SELECT status, superseded_at IS NOT NULL FROM app.publication WHERE id = %s",
            [incumbent_publication],
        ).fetchone()
        retired_candidate = connection.execute(
            "SELECT status, superseded_at IS NOT NULL FROM app.publication WHERE id = %s",
            [candidate_publication],
        ).fetchone()
        current_items = connection.execute(
            """SELECT count(*) FROM app.current_publication_item
               WHERE scope IN ('options-radar', concat('options-paper-incumbent:', %s),
                               concat('options-paper-experiment:', %s))""",
            [legacy, candidate],
        ).fetchone()[0]
        retired_shadow = connection.execute(
            "SELECT status, pending_entry_reason FROM analysis.shadow_trade WHERE id = %s",
            [shadow],
        ).fetchone()
        retired_candidate_shadow = connection.execute(
            "SELECT status, pending_entry_reason FROM analysis.shadow_trade WHERE id = %s",
            [candidate_shadow],
        ).fetchone()
    assert rows == [
        ('options-radar-core__agent_legacy', 1, 'superseded', 'options_radar', 'option-professional-v3-ticket', 'option-professional-v2'),
        ('options-radar-core', 3, 'superseded', 'options_radar', 'option-professional-v3-ticket', 'option-professional-v2'),
        ('options-radar-core', 4, 'active', 'options_radar', 'option-professional-v3-ticket', 'option-professional-v3-ticket'),
    ]
    assert retired_publication == ('superseded', True)
    assert retired_incumbent == ('superseded', True)
    assert retired_candidate == ('superseded', True)
    assert current_items == 0
    assert retired_shadow == ('unfilled', 'candidate_authority_changed')
    assert retired_candidate_shadow == ('unfilled', 'candidate_authority_changed')


@pytest.mark.parametrize('parameters', [
    {'feature_version': 'option-professional-v2', 'contract_version': 3},
    {'feature_version': 'option-professional-v3-ticket', 'contract_version': '3'},
])
def test_options_radar_binding_repair_rejects_invalid_active_v4_parameters(postgres_dsn, parameters):
    upgrade_database(postgres_dsn, '20260919_0025')
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """INSERT INTO analysis.strategy_revision
               (strategy_key, revision, name, status, parameters, authority_group,
                implementation_id, implementation_version, promoted_at)
               VALUES ('options-radar-core', 4, 'Invalid successor', 'active', %s,
                       'options-radar-core', 'options_radar', 'option-professional-v3-ticket', now())""",
            [Jsonb(parameters)],
        )

    with pytest.raises(ProgrammingError, match='incompatible immutable binding'):
        upgrade_database(postgres_dsn)

    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == '20260919_0025'
        assert connection.execute(
            "SELECT status FROM analysis.strategy_revision WHERE strategy_key = 'options-radar-core' AND revision = 4",
        ).fetchone()[0] == 'active'


def test_options_radar_orphan_candidate_repair_migration_supersedes_detached_candidate(postgres_dsn):
    upgrade_database(postgres_dsn, '20260920_0026')
    with psycopg.connect(postgres_dsn) as connection:
        parent = connection.execute(
            """INSERT INTO analysis.strategy_revision
               (strategy_key, revision, name, status, parameters, authority_group,
                implementation_id, implementation_version)
               VALUES ('options-radar-orphan-parent', 1, 'Retired parent', 'superseded', '{}',
                       'options-radar-core', 'unavailable', '1') RETURNING id""",
        ).fetchone()[0]
        candidate = connection.execute(
            """INSERT INTO analysis.strategy_revision
               (strategy_key, revision, name, status, parameters, supersedes_id, authority_group,
                implementation_id, implementation_version)
               VALUES ('options-radar-orphan-candidate', 1, 'Detached candidate', 'candidate', '{}', %s,
                       'options-radar-core', 'unavailable', '1') RETURNING id""",
            [parent],
        ).fetchone()[0]
        run = connection.execute(
            """INSERT INTO analysis.run
               (run_type, input_cutoff, code_version, feature_versions, strategy_revision_id,
                input_hash, started_at, finished_at, status)
               VALUES ('options-paper-experiment', now(), 'orphan-test', '{}', %s, %s,
                       now(), now(), 'succeeded') RETURNING id""",
            [candidate, 'f' * 64],
        ).fetchone()[0]
        publication = connection.execute(
            """INSERT INTO app.publication (scope, analysis_run_id, status, published_at)
               VALUES (concat('options-paper-experiment:', %s), %s, 'published', now()) RETURNING id""",
            [candidate, run],
        ).fetchone()[0]

    upgrade_database(postgres_dsn)

    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT status FROM analysis.strategy_revision WHERE id = %s", [candidate],
        ).fetchone()[0] == 'superseded'
        assert connection.execute(
            "SELECT status FROM app.publication WHERE id = %s", [publication],
        ).fetchone()[0] == 'superseded'


def test_strategy_definition_policy_upgrade_flushes_revision_trigger_events(postgres_dsn):
    upgrade_database(postgres_dsn, '20260909_0009')
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """INSERT INTO analysis.strategy_revision
               (strategy_key, revision, name, status, parameters, authority_group,
                implementation_id, implementation_version, p3_enabled)
               VALUES ('migration-trigger-test', 1, 'Migration trigger test', 'candidate', %s,
                       'migration-test', 'retired-implementation', '1', false)""",
            [Jsonb({})],
        )

    upgrade_database(postgres_dsn)

    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD_REVISION
        row = connection.execute(
            """SELECT definition_blockers, implementation_id, implementation_version, p3_enabled
                 FROM analysis.strategy_revision
                WHERE strategy_key = 'migration-trigger-test'""",
        ).fetchone()
        assert row == ([], "retired-implementation", "1", False)


def test_continuous_advisor_schema_is_append_only_and_advisory_only(postgres_dsn):
    upgrade_database(postgres_dsn)
    tables = (
        'analysis.continuous_advisor_packet',
        'analysis.continuous_advisor_response',
        'analysis.continuous_advisor_forecast_claim',
        'analysis.continuous_advisor_forecast_outcome',
        'analysis.continuous_advisor_prompt_version',
        'analysis.continuous_advisor_evaluation_cohort',
        'analysis.continuous_advisor_promotion_decision',
    )
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute('SET LOCAL ROLE market_app')
        for table in tables:
            assert connection.execute('SELECT has_table_privilege(current_user,%s,\'SELECT\')', [table]).fetchone()[0]
            assert not connection.execute('SELECT has_table_privilege(current_user,%s,\'INSERT\')', [table]).fetchone()[0]
            has_update = connection.execute('SELECT has_table_privilege(current_user,%s,\'UPDATE\')', [table]).fetchone()[0]
            assert has_update is (table == 'analysis.continuous_advisor_packet')
            assert not connection.execute('SELECT has_table_privilege(current_user,%s,\'DELETE\')', [table]).fetchone()[0]
        assert connection.execute(
            "SELECT has_function_privilege(current_user, 'analysis.write_continuous_advisor_promotion(jsonb)', 'EXECUTE')"
        ).fetchone()[0]


def test_baseline_supports_separate_login_and_nologin_application_group(postgres_dsn, monkeypatch):
    monkeypatch.setenv('MARKET_APP_LOGIN_ROLE', 'baseline_app_login')
    monkeypatch.delenv('MARKET_APP_DATABASE_PASSWORD', raising=False)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("CREATE ROLE baseline_app_login LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION")
        exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname='market_app'").fetchone()
        if exists:
            connection.execute('ALTER ROLE market_app NOLOGIN')
    try:
        upgrade_database(postgres_dsn)
        with psycopg.connect(postgres_dsn) as connection:
            assert connection.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname='market_app'").fetchone()[0] is False
            assert connection.execute("SELECT pg_has_role('baseline_app_login','market_app','MEMBER')").fetchone()[0]
    finally:
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute('DROP ROLE baseline_app_login')
            if exists:
                connection.execute('ALTER ROLE market_app LOGIN')


def test_fresh_baseline_rejects_unsafe_default_grants_before_secrets(postgres_dsn):
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO market_app")
        connection.execute("ALTER DEFAULT PRIVILEGES GRANT EXECUTE ON FUNCTIONS TO market_app")
    try:
        with pytest.raises(RuntimeError, match='fresh schema failed baseline verification'):
            upgrade_database(postgres_dsn)
        with psycopg.connect(postgres_dsn) as connection:
            assert connection.execute("SELECT to_regclass('analysis.phase4_allocation_signing_secret')").fetchone()[0] is None
    finally:
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute("ALTER DEFAULT PRIVILEGES REVOKE SELECT ON TABLES FROM market_app")
            connection.execute("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM market_app")
    upgrade_database(postgres_dsn)


def test_runtime_revision_import_does_not_load_checkout_migration_modules():
    import subprocess
    import sys

    code = """
import importlib.abc, sys
class NoMigrationAssets(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'migrations' or fullname.startswith('migrations.'):
            raise ImportError('migration assets unavailable in wheel')
sys.meta_path.insert(0, NoMigrationAssets())
from investment_panel.infrastructure.postgres.panel_models import QUERY_POLICIES
from investment_panel.application.read_models import loaders
assert QUERY_POLICIES
"""
    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True)
