from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from app import data_access
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.jobs.update_market_data import _market_metrics_row
from investment_panel.providers.yfinance_provider import return_on_invested_capital


# Regression: ISSUE-001 — Watchlist valuation and quality columns were empty.
# Found by /qa on 2026-07-12
# Report: .gstack/qa-reports/qa-report-127-0-0-1-2026-07-12.md
def test_market_metrics_are_composed_into_watchlist_screen(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    repository.register_source(
        "daily-market-prices",
        name="Daily market prices",
        family="market_data",
        kind="daily_bars",
        capabilities={"price_bars": True, "market_metrics": True},
    )
    run_id = repository.start_run("daily-market-prices", "price_bars")
    observed_at = datetime(2026, 7, 12, 20, tzinfo=UTC)
    info = {
        "shortName": "Example Holdings",
        "marketCap": 10_000_000_000,
        "currentPrice": 50,
        "trailingPE": None,
        "forwardPE": -12,
        "priceToSalesTrailing12Months": 4.5,
        "totalRevenue": 2_000_000_000,
        "revenueGrowth": 0.22,
        "profitMargins": -0.08,
        "freeCashflow": 500_000_000,
        "returnOnInvestedCapital": 0.18,
    }
    try:
        repository.store_price_bars(
            run_id,
            "daily-market-prices",
            [{"symbol": "EXM", "date": "2026-07-10", "open": 49, "high": 51, "low": 48, "close": 50, "volume": 1_000_000}],
        )
        repository.store_fundamental_observations(
            run_id,
            "daily-market-prices",
            "market_metrics",
            [_market_metrics_row("EXM", "equity", info, observed_at)],
        )
        repository.finish_run(run_id, "succeeded")
        with runtime.transaction() as connection:
            instrument_id = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = 'EXM'"
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO app.watchlist_item (instrument_id, watch_state) VALUES (%s, 'watched')",
                [instrument_id],
            )

        panel = data_access.load_table_panel_data(
            {"database": {"url": migrated_postgres_dsn}}, "universe_screen"
        )
        row = panel.rows("universe_screen")[0]

        assert row["name"] == "Example Holdings"
        assert row["market_cap"] == 10_000_000_000
        assert row["ps_ratio"] == 4.5
        assert row["pe_ratio"] is None
        assert row["pe_status"] == "not_meaningful"
        assert row["forward_pe"] is None
        assert row["forward_pe_status"] == "not_meaningful"
        assert row["fcf_yield"] == 0.05
        assert row["roic"] == 18.0
    finally:
        runtime.close()


# Regression: ISSUE-001 — ROIC needs a real invested-capital denominator.
# Found by /qa on 2026-07-12
# Report: .gstack/qa-reports/qa-report-127-0-0-1-2026-07-12.md
def test_roic_uses_nopat_over_average_invested_capital() -> None:
    income = pd.DataFrame(
        {"2025": [120.0, 0.25]},
        index=["Operating Income", "Tax Rate For Calcs"],
    )
    balance = pd.DataFrame(
        {"2025": [500.0], "2024": [400.0]},
        index=["Invested Capital"],
    )

    assert return_on_invested_capital(income, balance) == 0.2
