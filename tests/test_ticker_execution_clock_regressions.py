"""Exercise clock gates through the ticker execution owner, not just its helper."""
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from investment_panel.infrastructure.postgres.ticker_execution import TickerPaperExecutionRepository
from investment_panel.settings import AppConfig

NOW = datetime(2026, 9, 20, 15, tzinfo=UTC)


class Connection:
    def __init__(self, row):
        self.row = row
        self.writes = []

    def execute(self, sql, params=None):
        if 'FROM raw.confirmed_quote_at' in sql:
            return SimpleNamespace(fetchone=lambda: {
                'price': 100, 'quote_id': 1, 'source_id': 'test',
                'observed_at': NOW - timedelta(seconds=5), 'available_at': NOW,
            })
        if 'SELECT paper.id::text' in sql:
            return SimpleNamespace(fetchone=lambda: self.row)
        self.writes.append((sql, params))
        return SimpleNamespace(fetchone=lambda: None)


def order(**changes):
    return {'id': 'order-id', 'instrument_id': 1, 'created_at': NOW-timedelta(minutes=1),
            'status': 'open', 'quantity': 10, 'filled_quantity': 2, 'exited_quantity': 0,
            'expression_kind': 'STOCK', 'limit_price': 101, 'policy_result': {},
            'execution_quote': {}, **changes}


def test_ticker_stock_entry_cannot_fill_on_weekend():
    row = order(filled_quantity=0)
    connection = Connection(row)
    repo = TickerPaperExecutionRepository(None, AppConfig())
    result = repo._manage_entry(connection, row, NOW, 10)
    assert result['reason'] == 'market_closed_wait_for_next_session'
    assert not any('actual_fill_price' in sql or 'INSERT' in sql for sql, _ in connection.writes)


def test_expired_partial_entry_transitions_into_risk_management_same_tick(monkeypatch):
    row = order(expires_at=NOW-timedelta(seconds=1))
    connection = Connection(row)
    repo = TickerPaperExecutionRepository(None, AppConfig())
    managed = []
    def holding(conn, item, now, filled, exited):
        managed.append((item, filled, exited))
        return {'status': 'holding'}
    monkeypatch.setattr(repo, '_manage_open', holding)
    assert repo._manage_entry(connection, row, NOW, 8)['status'] == 'holding'
    assert managed[0][0]['policy_result']['entry_cancelled'] is True
    assert managed[0][1:] == (2, 0)
    assert any('policy_result' in sql for sql, _ in connection.writes)


def test_cancelled_partial_entry_does_not_retry_entry_on_following_ticks(monkeypatch):
    row = order(policy_result={'entry_cancelled': True}, expires_at=NOW-timedelta(seconds=1))
    connection = Connection(row)
    repo = TickerPaperExecutionRepository(SimpleNamespace(transaction=lambda *args: nullcontext(connection)), AppConfig())
    def no_entry(*args):
        raise AssertionError('Cancelled remainder must not freeze holding management')
    monkeypatch.setattr(repo, '_manage_entry', no_entry)
    monkeypatch.setattr(repo, '_manage_open', lambda *args: {'status': 'holding'})
    assert repo._manage_one(row['id'], NOW)['status'] == 'holding'
