"""Single-snapshot creation, data preservation, and runtime permissions."""

import hashlib
import json
from pathlib import Path

import psycopg
import pytest

from investment_panel.database.migrations import alembic_config, upgrade_database
from investment_panel.database.panel_models import QUERY_POLICIES
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
    assert [path.name for path in versions] == ['20260907_0006_baseline.py', '20260908_0007_continuous_advisor.py']
    sql_files = sorted((root / 'migrations' / 'baseline').glob('*.sql'))
    assert len(sql_files) == 27
    assert max(path.read_text().count('\n') for path in sql_files) < 1500
    sql = '\n'.join(path.read_text() for path in sql_files)
    assert 'ADD COLUMN' not in sql
    assert 'DROP COLUMN' not in sql
    assert 'ALTER COLUMN' not in sql
    assert 'ALTER INDEX' not in sql


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
from investment_panel.database.panel_models import QUERY_POLICIES
from app.data_access import loaders
assert QUERY_POLICIES
"""
    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True)
