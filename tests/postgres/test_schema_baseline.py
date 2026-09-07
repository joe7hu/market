"""Baseline creation, drift refusal, atomic adoption, and runtime permissions."""

import hashlib
import json

import psycopg
import pytest

from investment_panel.database.migrations import HEAD_REVISION, alembic_config, downgrade_database, upgrade_database
from investment_panel.database.panel_models import QUERY_POLICIES
from migrations.baseline_contract import BASELINE_REVISION, BASELINE_SCHEMA_HASHES, LEGACY_REVISION, LEGACY_SCHEMA_HASHES
from migrations.schema_contract import schema_contract


@pytest.fixture
def baseline_postgres_dsn(postgres_dsn):
    upgrade_database(postgres_dsn, BASELINE_REVISION)
    return postgres_dsn


def catalog_hash(connection):
    return hashlib.sha256(json.dumps(schema_contract(connection), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def legacy_shape(dsn):
    """Reverse only the reviewed schema delta, without replaying 131 migrations."""
    with psycopg.connect(dsn) as connection:
        connection.execute("""
            REVOKE ALL ON app.trade_journal,app.alert,analysis.event_decision_packet,
                analysis.event_scout_event,app.decision_truth FROM market_app;
            DROP INDEX analysis.ix_analysis_decision_recent_page;
            DROP INDEX analysis.ix_analysis_option_decision_contract_predecessor;
            CREATE INDEX ix_storage_archive_manifest_reference_source
                ON ops.storage_archive_manifest_reference(source_relation,source_row_id);
            CREATE INDEX ix_research_evidence_trial_result
                ON analysis.research_evidence_manifest(trial_result_id,evidence_kind);
            CREATE TABLE app.review_page_snapshot (
                id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
                model_name text NOT NULL, created_at timestamptz DEFAULT now() NOT NULL,
                expires_at timestamptz NOT NULL, rows jsonb NOT NULL);
            CREATE INDEX ix_review_page_snapshot_expires_at ON app.review_page_snapshot(expires_at);
        """)
        assert catalog_hash(connection) in LEGACY_SCHEMA_HASHES
        connection.execute("UPDATE public.alembic_version SET version_num=%s", [LEGACY_REVISION])


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


def test_adoption_preserves_user_records_and_secret(baseline_postgres_dsn):
    upgrade_database(baseline_postgres_dsn)
    with psycopg.connect(baseline_postgres_dsn) as connection:
        fresh_head_hash = catalog_hash(connection)
    downgrade_database(baseline_postgres_dsn, BASELINE_REVISION)
    with psycopg.connect(baseline_postgres_dsn) as connection:
        connection.execute("UPDATE catalog.instrument SET name='Keep my name' WHERE symbol='QQQ'")
        before = connection.execute("SELECT secret FROM analysis.phase4_allocation_signing_secret").fetchone()[0]
    legacy_shape(baseline_postgres_dsn)
    upgrade_database(baseline_postgres_dsn)
    upgrade_database(baseline_postgres_dsn)
    with psycopg.connect(baseline_postgres_dsn) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD_REVISION
        assert catalog_hash(connection) == fresh_head_hash
        assert connection.execute("SELECT name FROM catalog.instrument WHERE symbol='QQQ'").fetchone()[0] == 'Keep my name'
        assert connection.execute("SELECT secret FROM analysis.phase4_allocation_signing_secret").fetchone()[0] == before


def test_adoption_rejects_drift_before_changing_anything(baseline_postgres_dsn):
    legacy_shape(baseline_postgres_dsn)
    with psycopg.connect(baseline_postgres_dsn) as connection:
        connection.execute("ALTER TABLE catalog.instrument ADD COLUMN unexpected text")
    with pytest.raises(RuntimeError, match='adoption refused'):
        upgrade_database(baseline_postgres_dsn)
    with psycopg.connect(baseline_postgres_dsn) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == LEGACY_REVISION
        assert connection.execute("SELECT to_regclass('app.review_page_snapshot')").fetchone()[0]


def test_adoption_failure_rolls_back_and_can_retry(baseline_postgres_dsn, monkeypatch):
    from migrations import baseline_support

    legacy_shape(baseline_postgres_dsn)
    original = baseline_support.repair_legacy_schema
    def interrupted(connection):
        original(connection)
        raise RuntimeError('injected interruption')
    with monkeypatch.context() as patch:
        patch.setattr(baseline_support, 'repair_legacy_schema', interrupted)
        with pytest.raises(RuntimeError, match='injected interruption'):
            upgrade_database(baseline_postgres_dsn)
    with psycopg.connect(baseline_postgres_dsn) as connection:
        assert catalog_hash(connection) in LEGACY_SCHEMA_HASHES
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == LEGACY_REVISION
    upgrade_database(baseline_postgres_dsn)


def test_migration_lock_prevents_concurrent_runner_and_is_released(postgres_dsn):
    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(hashtextextended('market-schema-migration',0))")
        with pytest.raises(RuntimeError, match='another Market schema migration'):
            upgrade_database(postgres_dsn)
    upgrade_database(postgres_dsn)


def test_encoded_password_survives_alembic_config():
    dsn = 'postgresql://user:example%40password%25@localhost/market'
    assert alembic_config(dsn).get_main_option('sqlalchemy.url') == dsn.replace('postgresql://','postgresql+psycopg://')


@pytest.mark.parametrize('mutation', [
    'GRANT SELECT ON analysis.research_evaluator_signing_secret TO market_app',
    'GRANT SELECT(secret) ON analysis.research_evaluator_signing_secret TO market_app',
    'GRANT EXECUTE ON FUNCTION analysis.research_evaluator_signing_key() TO PUBLIC',
    'ALTER FUNCTION analysis.phase4_content_digest(jsonb) OWNER TO market_app',
    'ALTER SEQUENCE catalog.instrument_id_seq INCREMENT BY 2',
    'ALTER TABLE catalog.instrument DISABLE TRIGGER ALL',
    'ALTER TABLE raw.option_quote_default DISABLE TRIGGER ALL',
    'GRANT UPDATE,DELETE ON raw.option_quote_default TO market_app',
    'GRANT SELECT(bid) ON raw.option_quote_default TO market_app',
    'ALTER TABLE raw.option_quote_default OWNER TO market_app',
    'CREATE RULE discard_writes AS ON INSERT TO app.trade_journal DO INSTEAD NOTHING',
    'CREATE TRIGGER unexpected BEFORE INSERT ON raw.option_quote_default FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_review_item_guard()',
])
def test_adoption_rejects_unsafe_privileges_and_owners(baseline_postgres_dsn, mutation):
    legacy_shape(baseline_postgres_dsn)
    with psycopg.connect(baseline_postgres_dsn) as connection:
        connection.execute(mutation)
    with pytest.raises(RuntimeError, match='adoption refused'):
        upgrade_database(baseline_postgres_dsn)


def test_adoption_rejects_unsafe_role_membership(baseline_postgres_dsn):
    legacy_shape(baseline_postgres_dsn)
    with psycopg.connect(baseline_postgres_dsn) as connection:
        connection.execute('GRANT market_research_signer TO market_app')
    try:
        with pytest.raises(RuntimeError, match='unsafe role membership'):
            upgrade_database(baseline_postgres_dsn)
    finally:
        with psycopg.connect(baseline_postgres_dsn) as connection:
            connection.execute('REVOKE market_research_signer FROM market_app')


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
