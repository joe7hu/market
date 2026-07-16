from __future__ import annotations

from datetime import UTC, datetime, timedelta

from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.legacy_bootstrap import import_source_signals
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.source_facts import SourceFactRepository
from investment_panel.database.today_analysis import _option_item, refresh_today_publication
from app.data_access.portfolio_ledger import record_portfolio_transaction


def test_today_publication_separates_raw_quotes_from_decision_rows(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        config = {"database": {"url": migrated_postgres_dsn}}
        record_portfolio_transaction(
            config,
            {
                "symbol": "NVDA", "transaction_type": "opening_balance", "quantity": 2, "price": 100,
                "executed_at": "2026-07-01T00:00:00Z", "idempotency_key": "today-publication-nvda",
            },
        )

        ingestion = IngestionRepository(runtime)
        ingestion.register_source("test-quotes", name="Test quotes", family="test", kind="quote")
        run_id = ingestion.start_run("test-quotes", "quotes")
        ingestion.store_quotes(
            run_id,
            "test-quotes",
            [
                {"symbol": "NVDA", "observed_at": datetime(2026, 7, 11, 12, tzinfo=UTC), "price": 150},
                {"symbol": "NVDA", "observed_at": datetime(2026, 7, 12, 12, tzinfo=UTC), "price": 999},
            ],
        )
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE raw.quote SET available_at = %s WHERE ingest_run_id = %s",
                [datetime(2026, 7, 11, 12, 30, tzinfo=UTC), run_id],
            )
            connection.execute(
                "UPDATE raw.quote_confirmation SET fact_available_at = %s WHERE ingest_run_id = %s",
                [datetime(2026, 7, 11, 12, 30, tzinfo=UTC), run_id],
            )
        ingestion.finish_run(run_id, "succeeded", item_count=2, instrument_count=1)
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET finished_at = %s WHERE id = %s",
                [datetime(2026, 7, 11, 12, 31, tzinfo=UTC), run_id],
            )
        ingestion.register_source("test-content", name="Test content", family="news", kind="article")
        content_run = ingestion.start_run("test-content", "content")
        SourceFactRepository(runtime).store_content_items(
            content_run,
            "test-content",
            [{
                "source_key": "nvda-news", "title": "NVDA demand update",
                "summary": "Demand remains firm", "observed_at": datetime(2026, 7, 11, 12, tzinfo=UTC),
                "symbols": ["NVDA"], "metadata": {"legacy_id": "nvda-news"},
            }],
        )
        ingestion.finish_run(content_run, "succeeded", item_count=1, instrument_count=1)
        import_source_signals(runtime, [{
            "id": "signal-1", "source_item_id": "nvda-news", "source_id": "test-content",
            "symbol": "NVDA", "observed_at": datetime(2026, 7, 11, 12, tzinfo=UTC),
            "signal_type": "thesis", "sentiment": "bullish", "confidence": 0.8,
            "thesis": "Demand remains firm", "evidence_refs": "[]",
        }])

        result = refresh_today_publication(runtime, now=datetime(2026, 7, 11, 13, tzinfo=UTC))
        assert result["daily_brief"] == 3
        assert result["source_changes"] == 1
        publication = AnalysisRepository(runtime)
        brief = publication.publication_rows("today", "daily_brief")
        assert {row["category"] for row in brief} == {"decide_now", "portfolio_pulse", "whats_changed"}
        pulse = next(row for row in brief if row["category"] == "portfolio_pulse")
        assert pulse["market_value"] == 300
        assert pulse["unrealized_pnl"] == 100
        assert "provider_payload" not in pulse

        correction_run = ingestion.start_run("test-quotes", "quotes")
        ingestion.store_quotes(
            correction_run,
            "test-quotes",
            [{"symbol": "NVDA", "observed_at": datetime(2026, 7, 11, 12, tzinfo=UTC), "price": 160}],
        )
        ingestion.finish_run(correction_run, "succeeded", item_count=1, instrument_count=1)
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE raw.quote SET available_at = %s WHERE ingest_run_id = %s",
                [datetime(2026, 7, 11, 14, tzinfo=UTC), correction_run],
            )
            connection.execute(
                "UPDATE raw.quote_confirmation SET fact_available_at = %s WHERE ingest_run_id = %s",
                [datetime(2026, 7, 11, 14, tzinfo=UTC), correction_run],
            )
            connection.execute(
                "UPDATE ingest.run SET finished_at = %s WHERE id = %s",
                [datetime(2026, 7, 11, 14, tzinfo=UTC), correction_run],
            )
        refresh_today_publication(runtime, now=datetime(2026, 7, 11, 13, tzinfo=UTC))
        replayed = AnalysisRepository(runtime).publication_rows("today", "daily_brief")
        replayed_pulse = next(row for row in replayed if row["category"] == "portfolio_pulse")
        assert replayed_pulse["market_value"] == 300

        with runtime.read() as connection:
            assert connection.execute("SELECT count(*) AS count FROM raw.quote").fetchone()["count"] == 2
            validation = connection.execute(
                "SELECT validation FROM app.publication WHERE id = %s", [result["publication_id"]]
            ).fetchone()["validation"]
        assert validation["raw_and_analysis_separated"] is True
    finally:
        runtime.close()


def test_today_option_item_preserves_published_rationale() -> None:
    row = _option_item({"symbol": "NVDA", "top_reasons": ["liquidity_supported", "convexity_supported"]})
    assert row["summary"] == "liquidity_supported; convexity_supported"


def test_today_uses_available_same_day_daily_close_before_synthetic_session_marker(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    as_of = datetime(2026, 7, 11, 13, tzinfo=UTC)
    try:
        record_portfolio_transaction(
            {"database": {"url": migrated_postgres_dsn}},
            {
                "symbol": "7203.T", "transaction_type": "opening_balance", "quantity": 1, "price": 100,
                "executed_at": "2026-07-01T00:00:00Z", "idempotency_key": "today-daily-close",
            },
        )
        ingestion = IngestionRepository(runtime)
        ingestion.register_source("daily-close", name="Daily close", family="market_data", kind="daily_bars")
        run_id = ingestion.start_run("daily-close", "price_bars")
        ingestion.store_price_bars(
            run_id,
            "daily-close",
            [{"symbol": "7203.T", "date": "2026-07-11", "close": 120}],
            asset_classes={"7203.T": "equity"},
        )
        ingestion.finish_run(run_id, "succeeded", item_count=1, instrument_count=1)
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE raw.quote SET available_at = %s WHERE ingest_run_id = %s",
                [as_of - timedelta(minutes=30), run_id],
            )
            connection.execute(
                "UPDATE raw.quote_confirmation SET fact_available_at = %s WHERE ingest_run_id = %s",
                [as_of - timedelta(minutes=30), run_id],
            )
            connection.execute(
                "UPDATE ingest.run SET finished_at = %s WHERE id = %s",
                [as_of - timedelta(minutes=29), run_id],
            )

        refresh_today_publication(runtime, now=as_of)
        brief = AnalysisRepository(runtime).publication_rows("today", "daily_brief")
        pulse = next(row for row in brief if row["category"] == "portfolio_pulse")
        assert pulse["market_value"] == 120
    finally:
        runtime.close()


def test_today_source_changes_exclude_future_rows_and_preserve_source_diversity(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    as_of = datetime(2026, 7, 13, 13, tzinfo=UTC)
    try:
        record_portfolio_transaction(
            {"database": {"url": migrated_postgres_dsn}},
            {
                "symbol": "NVDA", "transaction_type": "opening_balance", "quantity": 2, "price": 100,
                "executed_at": "2026-07-01T00:00:00Z", "idempotency_key": "today-source-changes-nvda",
            },
        )
        ingestion = IngestionRepository(runtime)
        facts = SourceFactRepository(runtime)

        def add_source(source_id: str, count: int, observed_at: datetime) -> None:
            ingestion.register_source(source_id, name=source_id.title(), family="news", kind="article")
            run_id = ingestion.start_run(source_id, "content")
            rows = [
                {
                    "source_key": f"{source_id}-{index}",
                    "title": f"NVDA {source_id} update {index}",
                    "summary": f"NVDA evidence from {source_id}",
                    "observed_at": observed_at - timedelta(minutes=index),
                    "symbols": ["NVDA"],
                    "metadata": {"legacy_id": f"{source_id}-{index}"},
                }
                for index in range(count)
            ]
            facts.store_content_items(run_id, source_id, rows)
            ingestion.finish_run(run_id, "succeeded", item_count=count, instrument_count=1)
            import_source_signals(runtime, [
                {
                    "id": f"signal-{source_id}-{index}",
                    "source_item_id": f"{source_id}-{index}",
                    "source_id": source_id,
                    "symbol": "NVDA",
                    "observed_at": observed_at - timedelta(minutes=index),
                    "signal_type": "thesis",
                    "sentiment": "bullish",
                    "confidence": 0.8,
                    "thesis": f"NVDA evidence from {source_id}",
                    "evidence_refs": "[]",
                }
                for index in range(count)
            ])

        add_source("crowded", 20, as_of - timedelta(hours=1))
        add_source("second", 1, as_of - timedelta(hours=2))
        add_source("future", 20, as_of + timedelta(days=30))

        result = refresh_today_publication(runtime, now=as_of)
        source_rows = [
            row for row in AnalysisRepository(runtime).publication_rows("today", "daily_brief")
            if row.get("category") == "whats_changed"
        ]
    finally:
        runtime.close()

    assert result["source_changes"] == 3
    assert {row["source"] for row in source_rows} == {"Crowded", "Second"}
    assert sum(row["source"] == "Crowded" for row in source_rows) == 2
