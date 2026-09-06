from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from app.routers.panel import decision_inbox_queue
from investment_panel.database.decision_inbox import DecisionInboxRepository, evidence_fingerprint
from investment_panel.database.migrations import downgrade_database, upgrade_database
from investment_panel.database.runtime import DatabaseRuntime


@pytest.mark.parametrize('state', ['acknowledged', 'dismissed', 'review_complete', 'snoozed'])
def test_application_role_review_state_survives_reopen(application_postgres_dsn, state):
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    repository = DecisionInboxRepository(runtime)
    until = datetime.now(UTC) + timedelta(hours=1)
    item = repository.emit(event_type='ready', payload={'symbol': 'AAA'}, dedupe_key=str(uuid4()))
    projected = decision_inbox_queue(repository.rows(current_only=True)['items'])
    assert projected[0]['inbox_item_id'] == item['id']
    repository.set_user_state(item['id'], state=state, snoozed_until=until, dismiss_reason='Reviewed evidence')
    runtime.close()
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    repository = DecisionInboxRepository(runtime)
    try:
        with runtime.read() as connection:
            assert connection.execute('SELECT current_user AS role').fetchone()['role'] == 'market_app'
        assert repository.rows(current_only=True)['count'] == 0
        assert repository.rows()['items'][0]['user_state'] == state
        assert not repository.emit(event_type='ready', payload={'symbol': 'AAA'}, dedupe_key=item['dedupe_key'])['created']
        assert repository.rows(current_only=True)['count'] == 0
        if state == 'snoozed':
            with runtime.transaction() as connection:
                connection.execute("UPDATE app.decision_inbox_item SET snoozed_until = now() - interval '1 second' WHERE id = %s::uuid", [item['id']])
            assert repository.rows(current_only=True)['count'] == 1
    finally:
        runtime.close()


def test_outage_skips_obsolete_work_and_delivers_current_once(application_postgres_dsn):
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    repository = DecisionInboxRepository(runtime)
    now = datetime.now(UTC) + timedelta(seconds=1)
    sent = []
    try:
        for state in ['resolved', 'expired', 'superseded', 'acknowledged', 'dismissed', 'review_complete', 'snoozed', 'open']:
            item = repository.emit(event_type='ready', payload={'symbol': state, **({'expires_at': (now - timedelta(days=1)).isoformat()} if state == 'expired' else {}), **({'state_transition': 'superseded'} if state == 'superseded' else {})}, dedupe_key=state)
            if state == 'resolved':
                with runtime.transaction() as connection:
                    connection.execute("UPDATE app.decision_inbox_item SET status = 'resolved' WHERE id = %s::uuid", [item['id']])
            elif state in ['acknowledged', 'dismissed', 'review_complete', 'snoozed']:
                repository.set_user_state(item['id'], state=state, dismiss_reason='done', snoozed_until=now + timedelta(hours=1))
        # Known pre-call unavailability is safe to retry; all obsolete work is retained.
        assert repository.deliver_outbox(sender=None, dry_run=False, now=now)['failed'] == 1
        assert repository.deliver_outbox(sender=sent.append, dry_run=False, now=now + timedelta(minutes=1))['sent'] == 1
        assert len(sent) == 1
        assert repository.deliver_outbox(sender=sent.append, dry_run=False, now=now + timedelta(minutes=2))['sent'] == 0
        assert repository.rows()['count'] == 8
        assert sum(row['delivery_status'] == 'suppressed' for row in repository.rows()['items']) == 6
        assert repository.deliver_outbox(sender=sent.append, dry_run=False, now=now + timedelta(hours=2))['sent'] == 1
        assert len(sent) == 2
    finally:
        runtime.close()


def test_evidence_fingerprint_ignores_timestamps_but_tracks_input_revision():
    base = {
        'ticker': 'AAA', 'opportunity_episode_id': 'episode-1',
        'decision_revision': 'revision-1', 'policy_version': 'risk-policy.v2',
        'input_hash': 'inputs-1', 'published_at': '2026-09-06T12:00:00Z',
    }
    later = {**base, 'published_at': '2026-09-06T12:01:00Z'}
    changed = {**base, 'input_hash': 'inputs-2'}
    assert evidence_fingerprint(base) == evidence_fingerprint(later)
    assert evidence_fingerprint(base) != evidence_fingerprint(changed)


@pytest.mark.parametrize('crash', ['before_send', 'after_send', 'exception_after_send'])
def test_uncertain_delivery_never_blindly_retries(application_postgres_dsn, crash):
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    repository = DecisionInboxRepository(runtime)
    item = repository.emit(event_type='ready', payload={'symbol': 'AAA'}, dedupe_key=str(uuid4()))
    now = datetime.now(UTC) + timedelta(seconds=1)
    sent = []
    try:
        if crash == 'before_send':
            with runtime.transaction() as connection:
                connection.execute("UPDATE app.notification_outbox SET status = 'sending', updated_at = %s WHERE inbox_item_id = %s::uuid", [now, item['id']])
        else:
            def sender(message):
                sent.append(message)
                if crash == 'after_send':
                    raise KeyboardInterrupt('process death after relay acceptance')
                raise TimeoutError('response lost after relay acceptance')
            if crash == 'after_send':
                with pytest.raises(KeyboardInterrupt):
                    repository.deliver_outbox(sender=sender, dry_run=False, now=now)
            else:
                repository.deliver_outbox(sender=sender, dry_run=False, now=now)
        before = len(sent)
        repository.deliver_outbox(sender=sent.append, dry_run=False, now=now + timedelta(minutes=2))
        repository.deliver_outbox(sender=sent.append, dry_run=False, now=now + timedelta(days=1))
        assert len(sent) == before
        assert repository.rows()['items'][0]['delivery_status'] == 'uncertain'
    finally:
        runtime.close()


def test_notification_outcomes_raw_migration_round_trip(postgres_dsn):
    upgrade_database(postgres_dsn, '20260906_0127')
    upgrade_database(postgres_dsn)
    downgrade_database(postgres_dsn, '20260906_0127')
    upgrade_database(postgres_dsn)
    downgrade_database(postgres_dsn, '20260906_0127')
    with psycopg.connect(postgres_dsn) as connection:
        item = connection.execute("INSERT INTO app.decision_inbox_item (dedupe_key, event_type) VALUES ('migration', 'ready') RETURNING id").fetchone()[0]
        connection.execute("INSERT INTO app.notification_outbox (dedupe_key, inbox_item_id, event_type, status) VALUES ('migration', %s, 'ready', 'failed')", [item])
    upgrade_database(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        from investment_panel.database.migrations import HEAD_REVISION

        assert connection.execute('SELECT version_num FROM alembic_version').fetchone()[0] == HEAD_REVISION
        assert connection.execute('SELECT status FROM app.notification_outbox').fetchone()[0] == 'uncertain'
    with pytest.raises(Exception, match='reconcile terminal notification outcomes'):
        downgrade_database(postgres_dsn, '20260906_0127')
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute('SELECT status FROM app.notification_outbox').fetchone()[0] == 'uncertain'


def test_review_endpoint_refresh_and_authorization(application_postgres_dsn, monkeypatch):
    from fastapi.testclient import TestClient
    from app import dependencies
    from app.main import app
    from app.data_access import loaders
    from app.data_access.types import DataStatus, PanelData
    from conftest import typed_config

    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    repository = DecisionInboxRepository(runtime)
    item = repository.emit(event_type='ready', payload={'symbol': 'AAA'}, dedupe_key=str(uuid4()))
    monkeypatch.setattr(loaders, 'load_panel_scope_data', lambda *_args: PanelData(status=DataStatus(True, 'loaded', 'postgresql'), tables={}))
    app.dependency_overrides[dependencies.get_config] = lambda: typed_config(application_postgres_dsn)
    try:
        client = TestClient(app)
        before = client.get('/api/today')
        assert before.status_code == 200
        assert any(row.get('inbox_item_id') == item['id'] for row in before.json()['actions'])
        url = f"/api/decision-inbox/{item['id']}/state"
        assert client.post(url, json={'state': 'dismissed'}).status_code == 400
        assert client.post(url, json={'state': 'acknowledged'}, headers={'host': 'attacker.example'}).status_code == 403
        assert repository.rows(current_only=True)['count'] == 1
        assert client.post(url, json={'state': 'review_complete'}).status_code == 200
        refreshed = client.get('/api/today')
        assert refreshed.status_code == 200
        assert not any(row.get('inbox_item_id') == item['id'] for row in refreshed.json()['actions'])
        assert client.post(f'/api/decision-inbox/{uuid4()}/state', json={'state': 'acknowledged'}).status_code == 404
    finally:
        app.dependency_overrides.pop(dependencies.get_config, None)
        runtime.close()


def test_concurrent_delivery_claim_sends_once(application_postgres_dsn):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    repository = DecisionInboxRepository(runtime)
    repository.emit(event_type='ready', payload={'symbol': 'AAA'}, dedupe_key=str(uuid4()))
    started, release = Event(), Event()
    sent = []
    def sender(message):
        sent.append(message)
        started.set()
        assert release.wait(5)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(repository.deliver_outbox, sender=sender, dry_run=False)
            assert started.wait(5)
            try:
                second = workers.submit(repository.deliver_outbox, sender=sent.append, dry_run=False)
                assert second.result(timeout=5)['sent'] == 0
            finally:
                release.set()
            assert first.result(timeout=5)['sent'] == 1
        assert len(sent) == 1
    finally:
        release.set()
        runtime.close()


@pytest.mark.parametrize('table,column,value', [
    ('decision_inbox_item', 'status', 'resolved'),
    ('decision_inbox_item', 'user_state', 'review_complete'),
    ('decision_inbox_item', 'reviewed_at', datetime.now(UTC)),
    ('decision_inbox_item', 'created_at', datetime.now(UTC)),
    ('notification_outbox', 'status', 'sent'),
    ('notification_outbox', 'attempts', 9),
    ('notification_outbox', 'sent_at', datetime.now(UTC)),
    ('notification_outbox', 'updated_at', datetime.now(UTC)),
])
def test_application_cannot_insert_forged_audit_fields(application_postgres_dsn, table, column, value):
    from psycopg import sql

    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    try:
        repository = DecisionInboxRepository(runtime)
        item = repository.emit(event_type='ready', payload={'symbol': 'AAA'}, dedupe_key=str(uuid4()))
        columns, values = ['dedupe_key', 'event_type', column], [str(uuid4()), 'ready', value]
        if table == 'notification_outbox':
            columns.append('inbox_item_id')
            values.append(item['id'])
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with runtime.transaction() as connection:
                connection.execute(sql.SQL('INSERT INTO app.{} ({}) VALUES ({})').format(
                    sql.Identifier(table), sql.SQL(', ').join(map(sql.Identifier, columns)),
                    sql.SQL(', ').join(sql.Placeholder() for _ in values),
                ), values)
    finally:
        runtime.close()
