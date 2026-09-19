"""Economic freshness must not be rejuvenated by a later ingestion timestamp."""
from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.core.option_trade_ticket import build_option_trade_ticket, execution_policy

NOW = datetime(2026, 9, 18, 15, 30, tzinfo=UTC)


def leg(**overrides):
    return {"contract_id": "10", "option_type": "call", "side": "buy", "strike": 100,
            "bid": 1.0, "ask": 1.05, "bid_size": 10, "ask_size": 10, "open_interest": 500,
            "quote_time": NOW, "observed_at": NOW, "available_at": NOW, **overrides}


def policy(legs):
    return execution_policy(legs, structure="long_call", entry_price=1.05,
                            market_session="regular", evaluated_at=NOW)


@pytest.mark.parametrize("delay,blocked", [(0, False), (60, False), (120, False), (121, True), (900, True)])
def test_availability_does_not_refresh_old_observation(delay, blocked):
    blockers = policy([leg(observed_at=NOW - timedelta(seconds=delay))])["blockers"]
    assert ("quote_observation_stale" in blockers) is blocked


@pytest.mark.parametrize("clock", ["observed_at", "available_at"])
def test_explicit_missing_clock_is_not_replaced_by_quote_time(clock):
    assert "complete_quote_timestamps_required" in policy([leg(**{clock: None})])["blockers"]
    ticket = build_option_trade_ticket(decision_id="test", symbol="ABC", structure="long_call",
        expiration="2027-01-15", legs=[leg(**{clock: None})], entry_price=1.05,
        one_unit_max_loss=105, state="READY", evaluated_at=NOW, market_session="regular")
    assert "complete_quote_timestamps_required" in ticket["blockers"]
    assert ticket["legs"][0][clock] is None
    assert ticket["state"] != "READY"


def test_future_availability_and_inverted_observation_are_blocked():
    assert "quote_not_available_at_cutoff" in policy([leg(available_at=NOW + timedelta(seconds=1))])["blockers"]
    assert "quote_observation_after_availability" in policy([leg(available_at=NOW - timedelta(seconds=1))])["blockers"]


@pytest.mark.parametrize("delta,blocked", [(0, False), (5, False), (6, True)])
def test_interleg_observation_skew_is_not_hidden_by_batch_availability(delta, blocked):
    result = policy([leg(), leg(contract_id="11", observed_at=NOW - timedelta(seconds=delta))])
    assert ("interleg_skew_over_5_seconds" in result["blockers"]) is blocked


def test_legacy_single_clock_remains_supported_without_faking_a_new_clock():
    old = leg()
    old.pop("observed_at")
    old.pop("available_at")
    assert not policy([old])["blockers"]


@pytest.mark.parametrize("field", ["bid_size", "ask_size", "open_interest"])
@pytest.mark.parametrize("value", [True, float("inf"), float("nan"), 0.2, -1])
def test_ticket_does_not_coerce_malformed_liquidity_into_executable_size(field, value):
    ticket = build_option_trade_ticket(decision_id="test", symbol="ABC", structure="long_call",
        expiration="2027-01-15", legs=[leg(**{field: value})], entry_price=1.05,
        one_unit_max_loss=105, state="READY", evaluated_at=NOW, market_session="regular")
    assert ticket["legs"][0][field] is None
    assert ticket["state"] != "READY"


@pytest.mark.parametrize("clock", ["observed_at", "available_at", "quote_time"])
@pytest.mark.parametrize("value", [NOW.replace(tzinfo=None), "2026-09-18T15:30:00", "2026-09-18"])
def test_execution_never_assumes_timezone_for_naive_quote_evidence(clock, value):
    result = policy([leg(**{clock: value})])
    assert result["blockers"]
    ticket = build_option_trade_ticket(decision_id="test", symbol="ABC", structure="long_call",
        expiration="2027-01-15", legs=[leg(**{clock: value})], entry_price=1.05,
        one_unit_max_loss=105, state="READY", evaluated_at=NOW, market_session="regular")
    # Explicit economic and availability clocks are binding. quote_time is an
    # informational alias normalized from available_at when both are supplied.
    if clock != "quote_time":
        assert ticket["legs"][0][clock] is None


@pytest.mark.parametrize("field", ["bid_size", "ask_size", "open_interest"])
def test_direct_policy_does_not_accept_fractional_contract_counts(field):
    assert policy([leg(**{field: 100.5})])["blockers"]


def test_finite_quotes_with_overflowing_package_do_not_report_nan_slippage():
    result = policy([leg(bid=1.1e308, ask=1.2e308), leg(contract_id="11", bid=1.1e308, ask=1.2e308)])
    assert "finite_quote_package_required" in result["blockers"]
    assert result["expected_slippage"] is None


def test_a_single_finite_quote_does_not_overflow_midpoint():
    result = policy([leg(bid=1.1e308, ask=1.2e308)])
    assert not result["blockers"]
    assert result["expected_slippage"] > 0
