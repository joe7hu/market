from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
            {"symbol": symbol, "observed_at": start + timedelta(days=index), "price": base + index}
            for symbol, base in (("SPY", 500), ("QQQ", 450))
            for index in range(30)
        ]
        ingestion.store_quotes(run_id, "market-test", rows)
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
    finally:
        runtime.close()
