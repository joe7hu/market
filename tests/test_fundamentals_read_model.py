"""Regression tests for the fundamentals panel read model.

The global (no-symbol) load must keep one latest filing per symbol so dense
crypto daily snapshots cannot crowd equity quarterly filings out of the row
cap, while the per-symbol load must keep every period for YoY computations.
"""

from __future__ import annotations

import json

import duckdb
import pytest

from investment_panel.core.panel.read_market_data import fundamentals, valuations


@pytest.fixture()
def con() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE equity_fundamentals (
            symbol TEXT, period_end DATE, filing_date DATE, form_type TEXT,
            metrics JSON, source_url TEXT
        );
        CREATE TABLE crypto_fundamentals (
            symbol TEXT, date DATE, metrics JSON, source TEXT
        );
        """
    )
    # Two equities with quarterly filings from months ago.
    connection.execute(
        "INSERT INTO equity_fundamentals VALUES "
        "('MSFT', '2025-06-30', '2025-07-30', '10-K', ?, 'sec'),"
        "('MSFT', '2024-06-30', '2024-07-30', '10-K', ?, 'sec'),"
        "('LLY', '2025-03-31', '2025-05-01', '10-Q', ?, 'sec')",
        [json.dumps({"revenue": 281_724_000_000}), json.dumps({"revenue": 245_000_000_000}), json.dumps({"revenue": 12_000_000_000})],
    )
    # Crypto daily snapshots dated *today*, far more numerous and more recent
    # than the equity filings — the exact shape that crowded out equities.
    rows = []
    for day in range(1, 28):
        for sym in ("BTC-USD", "ETH-USD", "SOL-USD"):
            rows.append((sym, f"2026-06-{day:02d}", json.dumps({"price": 100}), "coingecko"))
    connection.executemany("INSERT INTO crypto_fundamentals VALUES (?, ?, ?, ?)", rows)
    return connection


def test_global_load_keeps_latest_per_symbol_and_includes_equities(con: duckdb.DuckDBPyConnection) -> None:
    rows = fundamentals(con)
    by_symbol: dict[str, int] = {}
    for row in rows:
        by_symbol[str(row["symbol"]).upper()] = by_symbol.get(str(row["symbol"]).upper(), 0) + 1

    # Equities are present despite the dense, more-recent crypto snapshots.
    assert {"MSFT", "LLY", "BTC-USD", "ETH-USD", "SOL-USD"} <= set(by_symbol)
    # Exactly one (latest) row per symbol on the global load.
    assert set(by_symbol.values()) == {1}
    msft = next(row for row in rows if row["symbol"] == "MSFT")
    assert str(msft["filing_date"]) == "2025-07-30"


def test_symbol_filtered_load_keeps_every_period(con: duckdb.DuckDBPyConnection) -> None:
    rows = fundamentals(con, symbols=["MSFT"])
    assert [str(row["filing_date"]) for row in rows] == ["2025-07-30", "2024-07-30"]


@pytest.fixture()
def valuation_con() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE valuation_models (
            symbol TEXT, as_of DATE, method TEXT, fair_value DOUBLE, upside_pct DOUBLE,
            assumptions JSON, diagnostics JSON
        );
        """
    )
    rows = []
    # A low-upside megacap plus many high-upside small caps sharing today's
    # as_of — the multi-method rows sorted by upside_pct that crowded the cap.
    rows.append(("MSFT", "2026-06-17", "blended_dcf_relative", 400.0, 1.5, "{}", "{}"))
    rows.append(("MSFT", "2026-06-17", "dcf_base_case", 380.0, -3.0, "{}", "{}"))
    rows.append(("MSFT", "2026-06-17", "relative_revenue_multiple", 420.0, 6.0, "{}", "{}"))
    for i in range(400):
        sym = f"SC{i:03d}"
        for method, upside in (("dcf_base_case", 90.0 + i), ("relative_revenue_multiple", 80.0 + i)):
            rows.append((sym, "2026-06-17", method, 10.0, upside, "{}", "{}"))
    connection.executemany("INSERT INTO valuation_models VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return connection


def test_valuations_global_load_keeps_one_per_symbol_and_includes_megacaps(valuation_con: duckdb.DuckDBPyConnection) -> None:
    rows = valuations(valuation_con)
    by_symbol: dict[str, list[str]] = {}
    for row in rows:
        by_symbol.setdefault(str(row["symbol"]).upper(), []).append(str(row["method"]))

    # The low-upside megacap survives despite 400 higher-upside small caps.
    assert "MSFT" in by_symbol
    # One representative row per symbol, preferring the blended method.
    assert by_symbol["MSFT"] == ["blended_dcf_relative"]
    assert all(len(methods) == 1 for methods in by_symbol.values())


def test_valuations_symbol_filtered_keeps_every_method(valuation_con: duckdb.DuckDBPyConnection) -> None:
    rows = valuations(valuation_con, symbols=["MSFT"])
    assert {str(row["method"]) for row in rows} == {
        "blended_dcf_relative",
        "dcf_base_case",
        "relative_revenue_multiple",
    }
