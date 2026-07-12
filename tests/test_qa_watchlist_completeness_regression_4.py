from __future__ import annotations

from app import data_access
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.runtime import DatabaseRuntime


# Regression: ISSUE-004 — The page left the percentile cell blank and mislabeled price history.
# Found by /qa on 2026-07-12
# Report: .gstack/qa-reports/qa-report-127-0-0-1-2026-07-12.md
def test_watchlist_exposes_an_explicit_one_year_price_percentile(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    repository.register_source(
        "daily-market-prices",
        name="Daily market prices",
        family="market_data",
        kind="daily_bars",
        capabilities={"price_bars": True},
    )
    run_id = repository.start_run("daily-market-prices", "price_bars")
    try:
        repository.store_price_bars(
            run_id,
            "daily-market-prices",
            [
                {"symbol": "RANK", "date": "2026-07-08", "open": 9, "high": 11, "low": 8, "close": 10, "volume": 100},
                {"symbol": "RANK", "date": "2026-07-09", "open": 19, "high": 21, "low": 18, "close": 20, "volume": 110},
                {"symbol": "RANK", "date": "2026-07-10", "open": 29, "high": 31, "low": 28, "close": 30, "volume": 120},
            ],
        )
        repository.finish_run(run_id, "succeeded")

        panel = data_access.load_table_panel_data(
            {"database": {"url": migrated_postgres_dsn}}, "technicals"
        )
        row = next(item for item in panel.rows("technicals") if item["symbol"] == "RANK")

        assert row["price_percentile_1y"] == 100
    finally:
        runtime.close()
