"""A new collector timestamp does not create a new executable observation."""
from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.domain.portfolio.paper_execution import consumed_quote_evidence, quote_consumption_blocker

NOW = datetime(2026, 9, 18, 15, tzinfo=UTC)


def order(**changes):
    return {'instrument_id': 1, 'created_at': NOW - timedelta(seconds=60), 'execution_quote': {}, **changes}


def quote(**changes):
    return {'contract_id': '12', 'quote_id': 'source-quote-1', 'source_id': 'test',
            'observed_at': NOW - timedelta(seconds=5), 'available_at': NOW - timedelta(seconds=4), **changes}


@pytest.mark.parametrize('phase', ['entry', 'exit'])
def test_closed_session_quote_cannot_fill_even_when_just_collected(phase):
    sunday = datetime(2026, 9, 20, 15, tzinfo=UTC)
    assert quote_consumption_blocker(order(created_at=sunday-timedelta(seconds=60)),
        [quote(observed_at=sunday-timedelta(seconds=5), available_at=sunday)], now=sunday, phase=phase) == 'market_closed_wait_for_next_session'


@pytest.mark.parametrize('changes,reason', [
    ({'observed_at': None}, 'paper_quote_clocks_missing'),
    ({'available_at': None}, 'paper_quote_clocks_missing'),
    ({'observed_at': NOW + timedelta(seconds=1)}, 'paper_quote_clock_conflict'),
    ({'available_at': NOW + timedelta(seconds=1)}, 'paper_quote_clock_conflict'),
    ({'observed_at': NOW - timedelta(seconds=121)}, 'fresh_regular_session_quote_required'),
    ({'observed_at': NOW - timedelta(seconds=61)}, 'post_order_quote_required'),
])
def test_ineligible_source_observations_are_named(changes, reason):
    assert quote_consumption_blocker(order(), [quote(**changes)], now=NOW, phase='entry') == reason


def test_first_valid_quote_is_consumed_atomically_and_cannot_fill_a_second_tick():
    row, quotes = order(), [quote()]
    assert quote_consumption_blocker(row, quotes, now=NOW, phase='entry') is None
    row['execution_quote'] = consumed_quote_evidence(row, quotes, now=NOW, phase='entry')
    assert quote_consumption_blocker(row, quotes, now=NOW+timedelta(seconds=1), phase='entry') == 'quote_already_consumed_wait_for_new_observation'
    assert quote_consumption_blocker(row, quotes, now=NOW+timedelta(seconds=1), phase='exit') == 'quote_already_consumed_wait_for_new_observation'
    # Reinserting the same provider event under a new row ID is not new liquidity.
    assert quote_consumption_blocker(row, [quote(quote_id='duplicate-ingest', available_at=NOW)], now=NOW, phase='entry') is not None


def test_each_leg_must_have_new_evidence_before_a_partial_order_can_refill():
    row = order()
    initial = [quote(), quote(contract_id='13', quote_id='source-quote-2')]
    row['execution_quote'] = consumed_quote_evidence(row, initial, now=NOW, phase='entry')
    changed = quote(quote_id='next-source-quote', observed_at=NOW+timedelta(seconds=1), available_at=NOW+timedelta(seconds=2))
    assert quote_consumption_blocker(row, [changed, initial[1]], now=NOW+timedelta(seconds=3), phase='entry') is not None
    assert quote_consumption_blocker(row, [changed, {**changed, 'contract_id': '13', 'quote_id': 'next-leg-2'}], now=NOW+timedelta(seconds=3), phase='entry') is None


def test_existing_fill_without_consumption_history_requires_a_post_fill_quote():
    row = order(filled_at=NOW)
    assert quote_consumption_blocker(row, [quote()], now=NOW+timedelta(seconds=1), phase='exit') == 'post_order_quote_required'


def test_preopen_quote_is_not_usable_at_regular_open():
    opened = datetime(2026, 9, 18, 13, 30, tzinfo=UTC)
    row = order(created_at=opened-timedelta(minutes=3))
    assert quote_consumption_blocker(row, [quote(observed_at=opened-timedelta(seconds=5), available_at=opened)], now=opened, phase='entry') == 'fresh_regular_session_quote_required'


def test_order_creation_evidence_is_required():
    assert quote_consumption_blocker(order(created_at=None), [quote()], now=NOW, phase='entry') == 'paper_order_clock_missing_or_invalid'


@pytest.mark.parametrize('evidence', [[], {'quote_consumption_v1': []},
    {'quote_consumption_v1': {'entry': {'quotes': ['not-a-record']}}}])
def test_corrupt_consumption_evidence_fails_closed(evidence):
    assert quote_consumption_blocker(order(execution_quote=evidence), [quote()], now=NOW, phase='entry') == 'paper_quote_consumption_invalid'
