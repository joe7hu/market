from __future__ import annotations

from investment_panel.core.options_intelligence import (
    build_expiry_signal,
    build_ticker_signal,
    unavailable_signals_for_source,
)


def _row(
    option_type: str,
    strike: float,
    bid: float,
    ask: float,
    iv: float,
    delta: float,
) -> dict[str, object]:
    return {
        "option_type": option_type,
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2,
        "iv": iv,
        "delta": delta,
        "observed_at": "2026-06-02T15:30:00Z",
    }


def _chain() -> list[dict[str, object]]:
    return [
        _row("put", 95, 1.9, 2.1, 0.40, -0.25),
        _row("call", 95, 6.8, 7.2, 0.33, 0.72),
        _row("put", 100, 3.9, 4.1, 0.36, -0.48),
        _row("call", 100, 4.9, 5.1, 0.34, 0.52),
        _row("put", 105, 7.8, 8.2, 0.39, -0.70),
        _row("call", 105, 2.9, 3.1, 0.32, 0.30),
    ]


def test_expiry_signal_uses_quote_and_chain_fields() -> None:
    signal = build_expiry_signal(
        "TSLA",
        "2026-06-05",
        "tradingview",
        _chain(),
        {"dte": 3, "contracts_count": 6},
        {"price": 100},
    )

    assert signal is not None
    assert signal["symbol"] == "TSLA"
    assert signal["atm_strike"] == 100.0
    assert round(signal["expected_move_pct"], 4) == 0.09
    assert round(signal["put_call_iv_skew"], 4) == 0.08
    assert signal["spread_quality"] == "tight"
    assert signal["contract_count"] == 6


def test_ticker_signal_selects_nearest_expiry_and_exposes_provider_limitations() -> None:
    expiry = build_expiry_signal(
        "NVDA",
        "2026-06-05",
        "tradingview",
        _chain(),
        {"dte": 3, "contracts_count": 6},
        {"price": 100},
    )
    assert expiry is not None

    ticker = build_ticker_signal("NVDA", "tradingview", [expiry])

    assert ticker["nearest_expiry"] == "2026-06-05"
    assert ticker["iv_regime"] == "normal"
    assert ticker["skew_signal"] == "put premium"
    assert {row["signal"] for row in ticker["unavailable_signals"]} >= {"open_interest", "volume"}


def test_option_signal_preserves_quote_availability_for_historical_selection() -> None:
    observed = "2026-06-02T15:30:00Z"
    expiry = build_expiry_signal(
        "NVDA",
        "2026-06-05",
        "robinhood",
        [{**row, "available_at": observed} for row in _chain()],
        {"dte": 3, "contracts_count": 6},
        {"price": 100},
    )

    assert expiry is not None
    assert expiry["available_at"] == observed
    assert build_ticker_signal("NVDA", "robinhood", [expiry])["available_at"] == observed


def test_unavailable_positioning_signals_are_source_scoped() -> None:
    tradingview = unavailable_signals_for_source("tradingview")
    other = unavailable_signals_for_source("robinhood")
    assert {row["signal"] for row in tradingview} >= {"open_interest", "volume"}
    assert {row["signal"] for row in other} == {
        "gex_regime",
        "call_wall",
        "put_wall",
        "gamma_flip",
        "max_pain",
        "unusual_volume",
    }


def test_empty_chain_has_no_signal() -> None:
    assert build_expiry_signal("TSLA", "2026-06-05", "tradingview", [], {"dte": 3}) is None
