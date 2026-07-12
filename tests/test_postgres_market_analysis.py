from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.data_access import load_panel_scope_data
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.market_analysis import refresh_market_publication
from investment_panel.database.runtime import DatabaseRuntime


def test_market_publication_builds_visible_models_from_normalized_quotes(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source("market-test", name="Market test", family="test", kind="quote")
        run_id = ingestion.start_run("market-test", "quotes")
        start = datetime(2026, 6, 1, 20, tzinfo=UTC)
        rows = [
            {
                "symbol": symbol, "date": (start + timedelta(days=index)).date(),
                "open": base + index, "high": base + index, "low": base + index,
                "close": base + index, "volume": 1,
            }
            for symbol, base in (("SPY", 500), ("QQQ", 450))
            for index in range(30)
        ]
        ingestion.store_price_bars(run_id, "market-test", rows, asset_classes={"SPY": "etf", "QQQ": "etf"})
        ingestion.finish_run(run_id, "succeeded", item_count=len(rows), instrument_count=2)

        result = refresh_market_publication(runtime, now=datetime(2026, 7, 1, 12, tzinfo=UTC))
        assert result["assets"] == 2
        assert result["drivers"] == 4
        repository = AnalysisRepository(runtime)
        assert {row["symbol"] for row in repository.publication_rows("market", "market_environment_assets")} == {"SPY", "QQQ"}
        assert {row["category"] for row in repository.publication_rows("market", "market_environment_model")} == {
            "Valuation", "Price Trend", "Market Breadth", "Risk Appetite"
        }

        panel = load_panel_scope_data({"database": {"url": migrated_postgres_dsn}}, "market")
        assert panel.status.ready is True
        assert len(panel.rows("market_environment_assets")) == 2
        assert len(panel.rows("market_environment_model")) == 4
        complete = load_panel_scope_data({"database": {"url": migrated_postgres_dsn}}, "dashboard")
        assert complete.status.ready is True
        assert complete.metadata["unavailable_models"] == []
        assert {row["symbol"] for row in complete.rows("technicals")} == {"SPY", "QQQ"}
        assert len(complete.rows("correlations")) == 1
    finally:
        runtime.close()


def test_market_publication_uses_prior_year_close_for_ytd(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source("ytd-test", name="YTD test", family="test", kind="quote")
        run_id = ingestion.start_run("ytd-test", "quotes")
        rows = [
            {"symbol": "SPY", "date": day, "open": price, "high": price, "low": price,
             "close": price, "volume": 1}
            for day, price in (
                (datetime(2025, 7, 1, tzinfo=UTC).date(), 100),
                (datetime(2025, 12, 31, tzinfo=UTC).date(), 120),
                (datetime(2026, 1, 2, tzinfo=UTC).date(), 121),
                (datetime(2026, 7, 1, tzinfo=UTC).date(), 132),
            )
        ]
        ingestion.store_price_bars(run_id, "ytd-test", rows)
        ingestion.finish_run(run_id, "succeeded")
        ingestion.register_source("ytd-override", name="YTD override", family="test", kind="quote")
        override_run = ingestion.start_run("ytd-override", "quotes")
        ingestion.store_price_bars(
            override_run,
            "ytd-override",
            [{"symbol": "SPY", "date": datetime(2026, 7, 1, tzinfo=UTC).date(),
              "open": 140, "high": 140, "low": 140, "close": 140, "volume": 1}],
        )
        ingestion.finish_run(override_run, "succeeded")

        refresh_market_publication(runtime, now=datetime(2026, 7, 1, 23, tzinfo=UTC))
        asset = AnalysisRepository(runtime).publication_rows("market", "market_environment_assets")[0]
        assert asset["return_ytd"] == pytest.approx(16.6666667)
        assert asset["return_1d"] == pytest.approx((140 / 121 - 1) * 100)
        assert asset["return_1y"] == pytest.approx(40.0)
        assert asset["source"] == "ytd-override"
    finally:
        runtime.close()
