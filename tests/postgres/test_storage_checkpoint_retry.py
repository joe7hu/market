"""Replay-idempotence and upgrade/downgrade safety against real PostgreSQL."""
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from investment_panel.domain.decision import build_ticker_decision
from investment_panel.infrastructure.postgres.instruments import reconcile_instrument
from investment_panel.infrastructure.postgres.migrations import downgrade_database, upgrade_database
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.infrastructure.postgres.ticker_decisions import TickerDecisionRepository


@pytest.fixture
def runtime(application_postgres_dsn):
    value = DatabaseRuntime(application_postgres_dsn)
    value.open()
    try:
        yield value
    finally:
        value.close()


def _state(runtime):
    with runtime.read() as connection:
        decision = connection.execute("""SELECT id, xmin::text, ctid::text,
            last_evaluated_at FROM analysis.ticker_decision""").fetchone()
        checkpoint = connection.execute("""SELECT xmin::text, ctid::text, evaluation_count,
            first_checked_at, last_checked_at FROM analysis.ticker_decision_checkpoint""").fetchone()
    return decision, checkpoint


def test_same_evaluation_retry_does_not_rewrite_decision_or_checkpoint(runtime):
    with runtime.transaction() as connection:
        reconcile_instrument(connection, "RETRY")
    at = datetime(2026, 9, 25, 14, tzinfo=UTC)
    tables = {"quotes": [{"symbol": "RETRY", "price": 100,
                          "observed_at": at - timedelta(minutes=1),
                          "available_at": at - timedelta(minutes=1), "confirmed": True}]}
    repository = TickerDecisionRepository(runtime)
    first = repository.publish(build_ticker_decision("RETRY", tables, as_of=at))
    assert first["status"] == "published"
    later = at + timedelta(minutes=1)
    result = repository.publish(build_ticker_decision("RETRY", tables, as_of=later))
    assert result["status"] == "unchanged"
    assert result["ticker_decision_id"] == first["ticker_decision_id"]
    before = _state(runtime)
    assert repository.publish(build_ticker_decision("RETRY", tables, as_of=later)) == result
    assert _state(runtime) == before
    latest = later + timedelta(minutes=1)
    repository.publish(build_ticker_decision("RETRY", tables, as_of=latest))
    after = _state(runtime)
    assert after[0]["id"] == before[0]["id"]
    assert after[0]["last_evaluated_at"] == latest
    assert after[1]["evaluation_count"] == before[1]["evaluation_count"] + 1
    assert after[1]["last_checked_at"] == latest


def test_projection_migration_round_trip_preserves_view_types_and_permissions(postgres_dsn):
    upgrade_database(postgres_dsn, "20260927_0041")

    def shape():
        with psycopg.connect(postgres_dsn) as connection:
            return connection.execute("""SELECT c.relname, a.attname, a.atttypid, a.atttypmod,
                has_table_privilege('market_app', c.oid, 'SELECT')
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                WHERE n.nspname = 'app' AND c.relname IN
                  ('option_publication_projection', 'current_publication_item_read', 'publication_content_item')
                ORDER BY c.relname, a.attnum""").fetchall()

    before = shape()
    upgrade_database(postgres_dsn)
    assert shape() == before
    downgrade_database(postgres_dsn, "20260927_0041")
    assert shape() == before
    upgrade_database(postgres_dsn)
    assert shape() == before


def test_audit_cli_never_mutates_even_with_execute_flag(runtime, monkeypatch):
    from types import SimpleNamespace
    from investment_panel.jobs import storage

    monkeypatch.setattr(storage, "_service", lambda _: SimpleNamespace(runtime=runtime))
    with pytest.raises(ValueError, match="read-only"):
        storage.run("audit", execute=True)
    assert storage.run("audit")["read_only"] is True
