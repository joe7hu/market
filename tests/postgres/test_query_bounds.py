from datetime import UTC, datetime, timedelta

import pytest

from conftest import typed_config
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.panel_models import load_postgres_tables
from investment_panel.database.panel_option_queries import current_option_query, transition_rows
from investment_panel.database.runtime import DatabaseRuntime


def test_current_option_panels_ignore_old_and_unfinished_captures(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source('bounds-options', name='Bounds', family='test', kind='option_chain',
                                  operational_state='active', health_owner='test', freshness_seconds=3600)
        for index, iv in enumerate((0.1, 0.7, 0.99)):
            run = ingestion.start_run('bounds-options', 'option_quotes')
            ingestion.store_option_snapshot(
                run, source_id='bounds-options', observed_at=datetime.now(UTC)-timedelta(days=3-index),
                market_session='regular', universe='bounds', rows=[{
                    'symbol': 'BOUND', 'expiration': '2027-01-15', 'strike': 10,
                    'option_type': 'call', 'bid': 1, 'ask': 2, 'mid': 1.5, 'iv': iv,
                }])
            if index < 2:
                ingestion.finish_run(run, 'succeeded')
        with runtime.transaction() as connection:
            connection.execute("""
                INSERT INTO raw.option_quote(snapshot_id,contract_id,observed_at,provider_iv)
                SELECT quote.snapshot_id,quote.contract_id,quote.observed_at-interval '1 second',0.2
                FROM raw.option_quote quote
                JOIN raw.option_snapshot snapshot ON snapshot.id=quote.snapshot_id
                WHERE snapshot.source_id='bounds-options' AND quote.provider_iv=0.7
            """)
        with runtime.read() as connection:
            chain = connection.execute(current_option_query('options_chain', scoped=True), [['BOUND']]).fetchall()
            surface = connection.execute(current_option_query('vol_surface_features', scoped=True), [['BOUND']]).fetchall()
        assert len(chain) == 1 and chain[0]['iv'] == 0.7
        assert len(surface) == 1 and surface[0]['contracts'] == 1 and surface[0]['call_iv'] == 0.7
        assert chain[0]['snapshot_id'] == surface[0]['snapshot_id']
        with runtime.transaction() as connection:
            generation = connection.execute("""
                INSERT INTO raw.option_capture_generation(snapshot_id,ingest_run_id,generation,capture_state)
                SELECT id,ingest_run_id,1,'complete' FROM raw.option_snapshot WHERE id=%s
                RETURNING id
            """, [chain[0]['snapshot_id']]).fetchone()['id']
            connection.execute('UPDATE raw.option_snapshot SET latest_complete_generation_id=%s WHERE id=%s',
                               [generation, chain[0]['snapshot_id']])
            connection.execute('UPDATE raw.option_quote SET capture_generation_id=%s WHERE snapshot_id=%s AND provider_iv=0.7',
                               [generation, chain[0]['snapshot_id']])
            connection.execute("""
                INSERT INTO raw.option_quote(snapshot_id,contract_id,observed_at,provider_iv)
                SELECT snapshot_id,contract_id,observed_at+interval '1 second',0.99
                FROM raw.option_quote WHERE snapshot_id=%s AND provider_iv=0.7
            """, [chain[0]['snapshot_id']])
        with runtime.read() as connection:
            selected = connection.execute(current_option_query('vol_surface_features', scoped=True), [['BOUND']]).fetchall()
        assert len(selected) == 1 and selected[0]['contracts'] == 1 and selected[0]['call_iv'] == 0.7
    finally:
        runtime.close()


def test_symbol_event_outside_global_prefix_remains_visible(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            connection.execute("""
                INSERT INTO analysis.event_decision_packet(event_id,symbol,event_kind,trigger_type,as_of)
                SELECT 'bound-' || n, CASE WHEN n=0 THEN 'BOUND' ELSE 'OTHER' END,
                       'test','test',now()-interval '1 day'+n*interval '1 second'
                FROM generate_series(0,201) n
            """)
        tables, _ = load_postgres_tables(typed_config(migrated_postgres_dsn), ['event_decision_packets'],
                                        query_symbol_filter={'BOUND'}, query_row_limits={'event_decision_packets': 1})
        assert len(tables['event_decision_packets']) == 1
        assert tables['event_decision_packets'][0]['symbol'] == 'BOUND'
    finally:
        runtime.close()


def test_transition_predecessor_is_outside_page_and_ties_are_stable(postgres_dsn):
    # Minimal relational fixture checks the actual query without the unrelated ingest contracts.
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(postgres_dsn, row_factory=dict_row) as connection:
        connection.execute('CREATE SCHEMA analysis; CREATE SCHEMA catalog')
        connection.execute('CREATE TABLE catalog.instrument(id bigint PRIMARY KEY,symbol text)')
        connection.execute('CREATE TABLE analysis.decision(id int PRIMARY KEY,instrument_id bigint,as_of timestamptz,state text,score float)')
        connection.execute('CREATE TABLE analysis.option_decision(decision_id int PRIMARY KEY,contract_id bigint)')
        connection.execute("INSERT INTO catalog.instrument VALUES(1,'BOUND')")
        connection.execute("INSERT INTO analysis.decision VALUES(1,1,now(),'WATCH',1),(2,1,now(),'READY',2),(3,1,now(),'FIRE',3)")
        connection.execute('INSERT INTO analysis.option_decision VALUES(1,10),(2,10),(3,10)')
        rows = transition_rows(connection, limit=1)
        assert len(rows) == 1 and rows[0]['candidate_event_id'] == '3'
        assert rows[0]['from_state'] == 'READY'


@pytest.mark.parametrize('empty_kind', ['history', 'radar', 'generation'])
def test_empty_latest_capture_never_revives_old_contracts(migrated_postgres_dsn, empty_kind):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source('empty-options', name='Empty', family='test', kind='option_chain',
                                  operational_state='active', health_owner='test', freshness_seconds=3600)
        for index in range(2):
            run = ingestion.start_run('empty-options', 'option_quotes')
            ingestion.store_option_snapshot(
                run, source_id='empty-options', observed_at=datetime.now(UTC)-timedelta(days=2-index),
                market_session='regular', universe='empty',
                history_symbol='EMPTY' if empty_kind == 'history' else None,
                collection_profile='history_full' if empty_kind == 'history' else 'radar',
                rows=[] if index == 1 and empty_kind != 'generation' else [{
                    'symbol': 'EMPTY', 'expiration': '2027-01-15', 'strike': 10,
                    'option_type': 'call', 'bid': 1, 'ask': 2, 'mid': 1.5, 'iv': 0.4,
                }])
            ingestion.finish_run(run, 'succeeded',
                                 summary={'symbols_requested': ['EMPTY']} if empty_kind == 'radar' else {})
        if empty_kind == 'generation':
            with runtime.transaction() as connection:
                generation = connection.execute("""
                    INSERT INTO raw.option_capture_generation(snapshot_id,ingest_run_id,generation,capture_state)
                    SELECT id,ingest_run_id,1,'complete' FROM raw.option_snapshot WHERE ingest_run_id=%s
                    RETURNING id,snapshot_id
                """, [run]).fetchone()
                connection.execute('UPDATE raw.option_snapshot SET latest_complete_generation_id=%s WHERE id=%s',
                                   [generation['id'], generation['snapshot_id']])
        with runtime.read() as connection:
            for name in ('options_chain', 'options_expiries', 'vol_surface_features'):
                rows = connection.execute(current_option_query(name, scoped=True), [['EMPTY']]).fetchall()
                assert rows == [], (empty_kind, name)
    finally:
        runtime.close()


@pytest.mark.parametrize('recorded_membership', [True, False])
def test_option_membership_probes_stop_at_latest_scoped_capture(migrated_postgres_dsn, recorded_membership):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source('probe-options', name='Probe', family='test', kind='option_chain',
                                  operational_state='active', health_owner='test', freshness_seconds=3600)
        for index in range(8):
            run = ingestion.start_run('probe-options', 'option_quotes')
            ingestion.store_option_snapshot(
                run, source_id='probe-options', observed_at=datetime.now(UTC)-timedelta(days=9-index),
                market_session='regular', universe='probe', rows=[{
                    'symbol': symbol, 'expiration': '2027-01-15', 'strike': 10,
                    'option_type': 'call', 'bid': 1, 'ask': 2, 'mid': 1.5, 'iv': 0.4,
                } for symbol in ('PROBE', 'UNRELATED')])
            ingestion.finish_run(run, 'succeeded', summary={
                'symbols_requested': ['PROBE', 'UNRELATED']
            } if recorded_membership else {})
        with runtime.read() as connection:
            plan = connection.execute('EXPLAIN (ANALYZE, FORMAT JSON) '+current_option_query('options_chain', scoped=True),
                                      [['PROBE']]).fetchone()['QUERY PLAN'][0]['Plan']
        def nodes(node):
            yield node
            for child in node.get('Plans', []):
                yield from nodes(child)
        membership = [node for node in nodes(plan) if node.get('Alias', '').startswith('member')]
        assert plan['Actual Rows'] == 1
        assert membership
        assert max(node['Actual Loops'] for node in membership) == (0 if recorded_membership else 1)
        assert all('snapshot_id' in node.get('Index Cond', '') and 'contract_id' in node.get('Index Cond', '')
                   for node in membership if node['Actual Loops'])
    finally:
        runtime.close()
