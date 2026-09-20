"""Nights, weekends and exchange holidays are not a pause in the news cycle."""
from datetime import UTC, date, datetime, timedelta

import pytest

from investment_panel.domain.decision import classify_freshness, market_deadline_passed, valuation_mark_is_stale


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


@pytest.mark.parametrize('observed,now', [
    ('2026-09-18T19:59:00', '2026-09-20T16:00:00'),  # ordinary weekend
    ('2026-09-04T19:59:00', '2026-09-08T12:00:00'),  # Labor Day, beyond the old 72h budget
    ('2026-11-27T17:59:00', '2026-11-29T16:00:00'),  # 13:00 early close
    ('2026-10-30T19:59:00', '2026-11-02T14:00:00'),  # DST changed over weekend
])
def test_last_session_values_survive_closures_but_news_ages(observed, now):
    observed, now = at(observed), at(now)
    assert not valuation_mark_is_stale(observed, observed + timedelta(minutes=2), now)
    assert classify_freshness('intraday_quote', observed, 'ok', False, now) == 'fresh'
    assert classify_freshness('news', observed, 'ok', False, now) == 'stale'


def test_weekend_news_update_is_fresh_without_a_new_price():
    now = at('2026-09-20T16:00:00')
    assert classify_freshness('news', now - timedelta(minutes=30), 'ok', False, now) == 'fresh'
    assert classify_freshness('news', now - timedelta(hours=4, seconds=1), 'ok', False, now) == 'stale'


@pytest.mark.parametrize('observed,now', [
    ('2026-09-17T19:59:00', '2026-09-20T16:00:00'),  # missed Friday
    ('2026-09-18T19:59:00', '2026-09-21T20:01:00'),  # missed Monday
    ('2026-09-18T19:59:00', '2026-09-21T18:01:00'),  # intraday budget spent
])
def test_no_indefinite_previous_session_valuation(observed, now):
    observed = at(observed)
    assert valuation_mark_is_stale(observed, observed, at(now))


def test_crypto_clock_never_pauses_for_equity_weekend():
    observed, now = at('2026-09-18T19:59:00'), at('2026-09-20T16:00:00')
    assert valuation_mark_is_stale(observed, observed, now, continuous=True)


@pytest.mark.parametrize('observed,available', [(None, None), (None, '2026-09-20T16:00:00'),
    ('2026-09-18T19:59:00', None), ('2026-09-20T17:00:00', '2026-09-20T17:00:00'),
    ('2026-09-18T19:59:00', '2026-09-18T19:58:00'), ('2026-09-18T19:59:00', '2026-09-21T16:00:00')])
def test_missing_future_or_reversed_mark_clocks_never_value_positions(observed, available):
    assert valuation_mark_is_stale(at(observed) if observed else None, at(available) if available else None, at('2026-09-20T16:00:00'))


@pytest.mark.parametrize('source', ['news', 'intraday_quote', 'options', 'daily', 'fundamental'])
def test_future_source_fact_is_unknown_not_fresh(source):
    now = at('2026-09-20T16:00:00')
    assert classify_freshness(source, now + timedelta(seconds=1), 'ok', False, now) == 'unknown'
    assert classify_freshness('news', now.replace(tzinfo=None), 'ok', False, now) == 'fresh'


def test_date_deadline_uses_session_close_including_early_close():
    assert not market_deadline_passed(date(2026, 11, 27), at('2026-11-27T17:59:59'))
    assert market_deadline_passed(date(2026, 11, 27), at('2026-11-27T18:00:00'))
    assert market_deadline_passed(at('2026-09-20T15:00:00'), at('2026-09-20T16:00:00'))
