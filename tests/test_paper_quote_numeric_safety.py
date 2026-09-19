"""Invalid numeric source facts must never become simulated fill prices."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from investment_panel.infrastructure.postgres.options_paper_quotes import latest_option_legs, package_price


@pytest.mark.parametrize("phase", ["entry", "exit"])
@pytest.mark.parametrize("field", ["bid", "ask"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", True, None])
def test_non_finite_or_missing_price_cannot_fill(phase, field, value):
    leg = {"side": "buy", "bid": 1.0, "ask": 1.1, field: value}
    assert package_price([leg], phase=phase) is None


@pytest.mark.parametrize("phase", ["entry", "exit"])
def test_finite_package_overflow_cannot_fill(phase):
    assert package_price([{"bid": 1e308, "ask": 1e308, "side": "buy"}] * 2, phase=phase) is None


@pytest.mark.parametrize("side,entry,exit_price", [("buy", 1.1, 1.0), ("sell", 1.0, 1.1)])
def test_valid_single_leg_uses_conservative_side(side, entry, exit_price):
    legs = [{"bid": 1.0, "ask": 1.1, "side": side}]
    assert package_price(legs, phase="entry") == entry
    assert package_price(legs, phase="exit") == exit_price


def test_valid_debit_spread_preserves_existing_cash_convention():
    legs = [{"bid": 3.0, "ask": 3.2, "side": "buy"}, {"bid": 1.0, "ask": 1.2, "side": "sell"}]
    assert package_price(legs, phase="entry") == 2.2
    assert package_price(legs, phase="exit") == 1.8


@pytest.mark.parametrize("bid,ask", [(0, 1), (-1, 1), (2, 1)])
def test_invalid_market_is_rejected(bid, ask):
    assert package_price([{"bid": bid, "ask": ask}], phase="entry") is None


@pytest.mark.parametrize("value", [float("inf"), float("nan"), 1.5, True])
def test_malformed_liquidity_is_unavailable_not_truncated(value):
    now = datetime(2026, 9, 18, 14, tzinfo=UTC)
    row = {"contract_id": 1, "option_type": "call", "strike": float("inf"),
           "bid": 1.0, "ask": 1.1, "bid_size": value, "ask_size": 2,
           "open_interest": value, "volume": 5, "available_at": now,
           "observed_at": now, "multiplier": 100}
    connection = SimpleNamespace(execute=lambda *_args: SimpleNamespace(fetchall=lambda: [row]))
    legs = latest_option_legs(connection, ticket_legs=[{"contract_id": 1}], as_of=now)
    assert legs[0]["bid_size"] is None
    assert legs[0]["open_interest"] is None
    assert legs[0]["strike"] is None
    assert legs[0]["ask_size"] == 2
