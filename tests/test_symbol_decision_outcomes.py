from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.symbol_decision_outcomes import SymbolDecisionOutcomeRepository


def test_stock_outcome_uses_only_price_facts_available_at_each_point_in_time(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    decision_at = datetime(2026, 6, 1, 21, 30, tzinfo=UTC)
    days = _trading_days(date(2026, 6, 1), 21)
    try:
        ingestion.register_source("daily", name="Daily", family="market", kind="daily_bars")
        for index, day in enumerate(days):
            available_at = datetime.combine(day, time(21), tzinfo=UTC)
            run = ingestion.start_run("daily", "price_bars", started_at=available_at)
            ingestion.store_price_bars(
                run,
                "daily",
                [
                    {"symbol": "ACME", "date": day.isoformat(), "close": 100 + index, "asset_class": "equity"},
                    {"symbol": "SPY", "date": day.isoformat(), "close": 500 + index * 0.5, "asset_class": "etf"},
                ],
            )
            ingestion.finish_run(run, "succeeded")
        with runtime.transaction() as connection:
            connection.execute("UPDATE ingest.run SET finished_at = started_at WHERE source_id = 'daily'")
            connection.execute(
                """
                UPDATE raw.price_bar
                SET available_at = ((trading_date::timestamp + time '21:00') AT TIME ZONE 'UTC')
                WHERE source_id = 'daily'
                """
            )
            connection.execute(
                """
                UPDATE raw.quote
                SET available_at = ((observed_at::date::timestamp + time '21:00') AT TIME ZONE 'UTC')
                WHERE source_id = 'daily'
                """
            )
            connection.execute("DELETE FROM raw.price_bar_confirmation")
            connection.execute("DELETE FROM raw.quote_confirmation")
            connection.execute(
                "INSERT INTO raw.price_bar_confirmation (fact_id, fact_available_at, ingest_run_id) SELECT id, available_at, ingest_run_id FROM raw.price_bar"
            )
            connection.execute(
                "INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id) SELECT id, available_at, ingest_run_id FROM raw.quote"
            )
            acme_id = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = 'ACME'").fetchone()["id"]
            connection.execute("UPDATE catalog.instrument SET sector = 'Technology' WHERE id = %s", [acme_id])
        analysis = AnalysisRepository(runtime)
        run_id = analysis.start_run(
            "equity_outcome_test", input_cutoff=decision_at, code_version="test",
            inputs={"symbol": "ACME", "as_of": decision_at.isoformat()},
        )
        with runtime.transaction() as connection:
            decision_id = connection.execute(
                """
                INSERT INTO analysis.decision
                    (run_id, decision_key, kind, instrument_id, as_of, state, input_hash)
                SELECT %s, 'ACME:outcome', 'equity', %s, %s, 'WATCH', input_hash
                FROM analysis.run WHERE id = %s
                RETURNING id::text
                """,
                [run_id, acme_id, decision_at, run_id],
            ).fetchone()["id"]
        analysis.finish_run(run_id, "succeeded")

        result = SymbolDecisionOutcomeRepository(runtime).refresh(now=datetime.combine(days[-1], time(21, 30), tzinfo=UTC))
        with runtime.read() as connection:
            outcome = connection.execute(
                "SELECT * FROM analysis.symbol_decision_outcome WHERE decision_id = %s::uuid",
                [decision_id],
            ).fetchone()

        assert result["resolved"] == 1
        assert outcome["state"] == "resolved"
        assert outcome["return_1d"] == pytest.approx(0.01)
        assert outcome["return_20d"] == pytest.approx(0.20)
        assert outcome["spy_adjusted_return_20d"] is not None
        assert outcome["sample_eligible"] is True
        assert outcome["metadata"]["paper_execution_supported"] is False
    finally:
        runtime.close()


def _trading_days(start: date, count: int) -> list[date]:
    output: list[date] = []
    current = start
    while len(output) < count:
        if current.weekday() < 5:
            output.append(current)
        current += timedelta(days=1)
    return output
