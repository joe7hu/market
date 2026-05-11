from __future__ import annotations

import json

from investment_panel.core.db import db, init_db
from investment_panel.core.panel import disclosures


def test_13f_disclosures_include_allocation_and_filing_history(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    first_raw = {
        "holdings_count": 2,
        "holdings_value_thousands": 1000,
        "holdings": [
            {"symbol": "AAA", "name": "AAA Inc", "value_thousands": 700, "shares_or_principal_amount": 70},
            {"symbol": "BBB", "name": "BBB Inc", "value_thousands": 300, "shares_or_principal_amount": 30},
        ],
    }
    second_raw = {
        "holdings_count": 2,
        "holdings_value_thousands": 2000,
        "holdings": [
            {"symbol": "BBB", "name": "BBB Inc", "value_thousands": 1200, "shares_or_principal_amount": 120},
            {"symbol": "AAA", "name": "AAA Inc", "value_thousands": 800, "shares_or_principal_amount": 80},
        ],
    }
    with db(db_path) as con:
        con.execute(
            "INSERT INTO disclosures VALUES (?, '13f', ?, ?, NULL, ?, ?, 'HOLDINGS', ?, ?, ?)",
            ["first", "Test 13F", "Test Filer", "2025-03-31", "2025-05-15", "1000", json.dumps(first_raw), "https://example.com/first"],
        )
        con.execute(
            "INSERT INTO disclosures VALUES (?, '13f', ?, ?, NULL, ?, ?, 'HOLDINGS', ?, ?, ?)",
            ["second", "Test 13F", "Test Filer", "2025-06-30", "2025-08-14", "2000", json.dumps(second_raw), "https://example.com/second"],
        )

        rows = disclosures(con)

    latest = next(row for row in rows if row["trader_name"] == "Test 13F" and str(row["event_date"]) == "2025-06-30")
    assert [holding["symbol"] for holding in latest["holding_sample"]] == ["BBB", "AAA"]
    assert [round(holding["weight"], 1) for holding in latest["holding_sample"]] == [60.0, 40.0]
    assert len(latest["allocation_history"]) == 2
    assert latest["allocation_history"][0]["symbol"] == "BBB"
    assert round(latest["allocation_history"][0]["weight_before"], 1) == 30.0
    assert round(latest["allocation_history"][0]["weight_after"], 1) == 60.0
    assert [point["date"] for point in latest["portfolio_history"]] == ["2025-03-31", "2025-06-30"]
    assert [point["value"] for point in latest["portfolio_history"]] == [1000.0, 2000.0]
