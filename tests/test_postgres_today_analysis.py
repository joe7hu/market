from __future__ import annotations

from datetime import UTC, datetime

from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.today_analysis import refresh_today_publication
from app.data_access.user_state import save_position


def test_today_publication_separates_raw_quotes_from_decision_rows(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        config = {"database": {"url": migrated_postgres_dsn}}
        save_position(config, {"symbol": "NVDA", "quantity": 2, "avg_cost": 100})

        ingestion = IngestionRepository(runtime)
        ingestion.register_source("test-quotes", name="Test quotes", family="test", kind="quote")
        run_id = ingestion.start_run("test-quotes", "quotes")
        ingestion.store_quotes(
            run_id,
            "test-quotes",
            [{"symbol": "NVDA", "observed_at": datetime(2026, 7, 11, 12, tzinfo=UTC), "price": 150}],
        )
        ingestion.finish_run(run_id, "succeeded", item_count=1, instrument_count=1)

        result = refresh_today_publication(runtime, now=datetime(2026, 7, 11, 13, tzinfo=UTC))
        assert result["daily_brief"] == 2
        publication = AnalysisRepository(runtime)
        brief = publication.publication_rows("today", "daily_brief")
        assert {row["category"] for row in brief} == {"decide_now", "portfolio_pulse"}
        pulse = next(row for row in brief if row["category"] == "portfolio_pulse")
        assert pulse["market_value"] == 300
        assert pulse["unrealized_pnl"] == 100
        assert "provider_payload" not in pulse

        with runtime.read() as connection:
            assert connection.execute("SELECT count(*) AS count FROM raw.quote").fetchone()["count"] == 1
            validation = connection.execute(
                "SELECT validation FROM app.publication WHERE id = %s", [result["publication_id"]]
            ).fetchone()["validation"]
        assert validation["raw_and_analysis_separated"] is True
    finally:
        runtime.close()
