from __future__ import annotations

from datetime import date, timedelta

from app import data_access
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.runtime import DatabaseRuntime


# Regression: ISSUE-002 — Watchlist history, momentum, volume, and ATR fields were empty.
# Found by /qa on 2026-07-12
# Report: .gstack/qa-reports/qa-report-127-0-0-1-2026-07-12.md
def test_daily_bars_are_composed_into_complete_watchlist_technicals(
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
    start = date(2025, 7, 1)
    rows = []
    for index in range(260):
        close = 100.0 + index
        rows.append(
            {
                "symbol": "HIST",
                "date": (start + timedelta(days=index)).isoformat(),
                "open": close - 1,
                "high": close + 2,
                "low": close - 2,
                "close": close,
                "volume": 1_000_000 + index * 1_000,
            }
        )
    try:
        assert repository.store_price_bars(
            run_id, "daily-market-prices", rows
        ) == len(rows)
        repository.finish_run(run_id, "succeeded")

        panel = data_access.load_table_panel_data(
            {"database": {"url": migrated_postgres_dsn}}, "technicals"
        )
        row = next(item for item in panel.rows("technicals") if item["symbol"] == "HIST")

        assert row["return_20d"] > 0
        assert row["return_60d"] > 0
        assert row["return_1y"] > 0
        assert row["drawdown_from_high"] < 0
        assert row["relative_volume_1m"] > 1
        assert row["atr_pct_1m"] > 0
        assert row["valuation_percentile"] == 100
        assert row["technical_score"] == 100
        assert len(row["chart_1y"]) == 252
        assert len(row["volume_1m_bars"]) == 22
        assert len(row["atr_pct_1m_points"]) == 22
        assert row["chart_1y"][-1]["close"] == 359
    finally:
        runtime.close()
