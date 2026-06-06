from __future__ import annotations

from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.free_sources import store_expiries, store_options_chain
from investment_panel.core.options_intelligence import clear_options_intelligence, record_tradingview_options_capabilities, refresh_options_intelligence
from investment_panel.core.panel import load_panel_data


REFERENCE_DATE = "2026-06-02"


def test_tradingview_options_intelligence_uses_available_chain_fields(tmp_path) -> None:
    db_path = tmp_path / "options.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-02T15:30:00Z', 100, 1, 1, 'USD', 'tradingview', '{}')"
        )
        record_tradingview_options_capabilities(con, "2026-06-02T15:30:00Z")
        store_expiries(con, "TSLA", "2026-06-02T15:30:00Z", [{"expiry": "2026-06-05", "dte": 3, "contracts_count": 6}])
        store_options_chain(
            con,
            "TSLA",
            "2026-06-02T15:30:00Z",
            [
                option_row("2026-06-05", 95, "put", 1.9, 2.1, 0.40, -0.25, "OPRA:TSLA260605P95.0"),
                option_row("2026-06-05", 95, "call", 6.8, 7.2, 0.33, 0.72, "OPRA:TSLA260605C95.0"),
                option_row("2026-06-05", 100, "put", 3.9, 4.1, 0.36, -0.48, "OPRA:TSLA260605P100.0"),
                option_row("2026-06-05", 100, "call", 4.9, 5.1, 0.34, 0.52, "OPRA:TSLA260605C100.0"),
                option_row("2026-06-05", 105, "put", 7.8, 8.2, 0.39, -0.70, "OPRA:TSLA260605P105.0"),
                option_row("2026-06-05", 105, "call", 2.9, 3.1, 0.32, 0.30, "OPRA:TSLA260605C105.0"),
            ],
        )

        counts = refresh_options_intelligence(con, ["TSLA"], reference_date=REFERENCE_DATE)
        expiry = query_rows(con, "SELECT * FROM options_expiry_signals WHERE symbol = 'TSLA'")[0]
        ticker = query_rows(con, "SELECT * FROM options_ticker_signals WHERE symbol = 'TSLA'")[0]
        capability = query_rows(con, "SELECT * FROM options_provider_capabilities WHERE provider = 'tradingview'")[0]

    assert counts == {"expiry_signals": 1, "ticker_signals": 1}
    assert round(expiry["expected_move_pct"], 4) == 0.09
    assert round(expiry["put_call_iv_skew"], 4) == 0.08
    assert expiry["spread_quality"] == "tight"
    assert ticker["iv_regime"] == "normal"
    assert ticker["skew_signal"] == "put premium"
    assert capability["supports_open_interest"] is False
    assert capability["supports_volume"] is False


def test_options_signal_tables_load_through_panel_contract(tmp_path) -> None:
    db_path = tmp_path / "panel-options.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('NVDA', '2026-06-02T15:30:00Z', 200, 1, 1, 'USD', 'tradingview', '{}')"
        )
        record_tradingview_options_capabilities(con, "2026-06-02T15:30:00Z")
        store_expiries(con, "NVDA", "2026-06-02T15:30:00Z", [{"expiry": "2026-06-05", "dte": 3, "contracts_count": 2}])
        store_options_chain(
            con,
            "NVDA",
            "2026-06-02T15:30:00Z",
            [
                option_row("2026-06-05", 200, "put", 4.8, 5.2, 0.45, -0.5, "OPRA:NVDA260605P200.0"),
                option_row("2026-06-05", 200, "call", 5.8, 6.2, 0.43, 0.5, "OPRA:NVDA260605C200.0"),
            ],
        )
        refresh_options_intelligence(con, ["NVDA"], reference_date=REFERENCE_DATE)

    panel = load_panel_data({"database": {"duckdb_path": str(db_path)}})
    assert panel["tables"]["options_provider_capabilities"][0]["supports_open_interest"] is False
    assert panel["tables"]["options_expiry_signals"][0]["symbol"] == "NVDA"
    assert panel["tables"]["options_ticker_signals"][0]["symbol"] == "NVDA"


def test_targeted_refresh_without_chain_rows_preserves_other_symbols(tmp_path) -> None:
    db_path = tmp_path / "targeted-options.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-02T15:30:00Z', 100, 1, 1, 'USD', 'tradingview', '{}')"
        )
        store_expiries(con, "TSLA", "2026-06-02T15:30:00Z", [{"expiry": "2026-06-05", "dte": 3, "contracts_count": 2}])
        store_options_chain(
            con,
            "TSLA",
            "2026-06-02T15:30:00Z",
            [
                option_row("2026-06-05", 100, "put", 3.9, 4.1, 0.36, -0.48, "OPRA:TSLA260605P100.0"),
                option_row("2026-06-05", 100, "call", 4.9, 5.1, 0.34, 0.52, "OPRA:TSLA260605C100.0"),
            ],
        )
        refresh_options_intelligence(con, ["TSLA"], reference_date=REFERENCE_DATE)

        counts = refresh_options_intelligence(con, ["NOCHAIN"], reference_date=REFERENCE_DATE)
        remaining = query_rows(con, "SELECT symbol FROM options_ticker_signals")

    assert counts == {"expiry_signals": 0, "ticker_signals": 0}
    assert remaining == [{"symbol": "TSLA"}]


def test_exchange_qualified_symbols_are_normalized_for_quote_join(tmp_path) -> None:
    db_path = tmp_path / "qualified-options.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('NVDA', '2026-06-02T15:30:00Z', 200, 1, 1, 'USD', 'tradingview', '{}')"
        )
        store_expiries(con, "NASDAQ:NVDA", "2026-06-02T15:30:00Z", [{"expiry": "2026-06-05", "dte": 3, "contracts_count": 2}])
        store_options_chain(
            con,
            "NASDAQ:NVDA",
            "2026-06-02T15:30:00Z",
            [
                option_row("2026-06-05", 200, "put", 4.8, 5.2, 0.45, -0.5, "OPRA:NVDA260605P200.0"),
                option_row("2026-06-05", 200, "call", 5.8, 6.2, 0.43, 0.5, "OPRA:NVDA260605C200.0"),
            ],
        )
        refresh_options_intelligence(con, ["NASDAQ:NVDA"], reference_date=REFERENCE_DATE)
        chain_symbols = query_rows(con, "SELECT DISTINCT symbol FROM options_chain")
        signal = query_rows(con, "SELECT symbol, expected_move_pct FROM options_ticker_signals")[0]

    assert chain_symbols == [{"symbol": "NVDA"}]
    assert signal["symbol"] == "NVDA"
    assert round(signal["expected_move_pct"], 4) == 0.055


def test_expired_chain_rows_do_not_drive_ticker_signal(tmp_path) -> None:
    db_path = tmp_path / "expired-options.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-02T15:30:00Z', 100, 1, 1, 'USD', 'tradingview', '{}')"
        )
        store_expiries(con, "TSLA", "2026-05-10T15:30:00Z", [{"expiry": "2026-05-11", "dte": 1, "contracts_count": 2}])
        store_options_chain(
            con,
            "TSLA",
            "2026-05-10T15:30:00Z",
            [
                option_row("2026-05-11", 100, "put", 9.8, 10.2, 0.90, -0.5, "OPRA:TSLA260511P100.0"),
                option_row("2026-05-11", 100, "call", 9.8, 10.2, 0.88, 0.5, "OPRA:TSLA260511C100.0"),
            ],
        )
        store_expiries(con, "TSLA", "2026-06-02T15:30:00Z", [{"expiry": "2026-06-05", "dte": 3, "contracts_count": 2}])
        store_options_chain(
            con,
            "TSLA",
            "2026-06-02T15:30:00Z",
            [
                option_row("2026-06-05", 100, "put", 3.9, 4.1, 0.36, -0.48, "OPRA:TSLA260605P100.0"),
                option_row("2026-06-05", 100, "call", 4.9, 5.1, 0.34, 0.52, "OPRA:TSLA260605C100.0"),
            ],
        )

        counts = refresh_options_intelligence(con, ["TSLA"], reference_date="2026-06-02")
        expiries = query_rows(con, "SELECT expiry FROM options_expiry_signals WHERE symbol = 'TSLA'")
        ticker = query_rows(con, "SELECT nearest_expiry, atm_iv, expected_move_pct FROM options_ticker_signals WHERE symbol = 'TSLA'")[0]

    assert counts == {"expiry_signals": 1, "ticker_signals": 1}
    assert [str(row["expiry"]) for row in expiries] == ["2026-06-05"]
    assert str(ticker["nearest_expiry"]) == "2026-06-05"
    assert round(ticker["atm_iv"], 2) == 0.35
    assert round(ticker["expected_move_pct"], 2) == 0.09


def test_clear_options_intelligence_removes_only_target_symbol(tmp_path) -> None:
    db_path = tmp_path / "clear-options.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        for symbol in ["TSLA", "NVDA"]:
            con.execute(
                "INSERT INTO quotes_intraday VALUES (?, '2026-06-02T15:30:00Z', 100, 1, 1, 'USD', 'tradingview', '{}')",
                [symbol],
            )
            store_expiries(con, symbol, "2026-06-02T15:30:00Z", [{"expiry": "2026-06-05", "dte": 3, "contracts_count": 2}])
            store_options_chain(
                con,
                symbol,
                "2026-06-02T15:30:00Z",
                [
                    option_row("2026-06-05", 100, "put", 3.9, 4.1, 0.36, -0.48, f"OPRA:{symbol}260605P100.0"),
                    option_row("2026-06-05", 100, "call", 4.9, 5.1, 0.34, 0.52, f"OPRA:{symbol}260605C100.0"),
                ],
            )
        refresh_options_intelligence(con, ["TSLA", "NVDA"], reference_date=REFERENCE_DATE)

        clear_options_intelligence(con, ["NASDAQ:TSLA"])
        remaining = query_rows(con, "SELECT symbol FROM options_ticker_signals ORDER BY symbol")

    assert remaining == [{"symbol": "NVDA"}]


def option_row(expiry: str, strike: float, option_type: str, bid: float, ask: float, iv: float, delta: float, symbol: str) -> dict[str, object]:
    return {
        "expiry": expiry,
        "dte": 3,
        "strike": strike,
        "type": option_type,
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2,
        "iv": iv,
        "delta": delta,
        "gamma": 0.01,
        "theta": -0.1,
        "vega": 0.2,
        "rho": 0.01,
        "theo": (bid + ask) / 2,
        "bid_iv": iv - 0.01,
        "ask_iv": iv + 0.01,
        "symbol": symbol,
    }
