from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime

import psycopg

from app.data_access.postgres_panel import load_postgres_tables
from investment_panel.core.brokers.types import BrokerSnapshot, ProviderStatus
from investment_panel.database.brokers import BrokerRepository
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.migrations import upgrade_database
from investment_panel.database.options_analysis import refresh_options_radar
from investment_panel.database.runtime import DatabaseRuntime


def test_broker_snapshot_recommendation_and_paper_order_are_postgresql_native(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    observed_at = datetime(2026, 7, 11, 12, 15, tzinfo=UTC)
    repository = BrokerRepository(runtime)
    try:
        result = repository.sync_snapshot(
            BrokerSnapshot(
                status=ProviderStatus(
                    provider="ibkr",
                    status="ok",
                    detail="paper connected",
                    checked_at=observed_at,
                    account_id="DU123",
                    account_mode="paper",
                    last_data_at=observed_at,
                    capabilities=["accounts", "positions", "quotes"],
                ),
                accounts=[
                    {
                        "account_id": "DU123",
                        "account_mode": "paper",
                        "currency": "USD",
                        "cash": 10_000,
                        "buying_power": 20_000,
                        "net_liquidation": 30_000,
                        "updated_at": observed_at,
                    }
                ],
                positions=[
                    {
                        "account_id": "DU123",
                        "symbol": "NVDA",
                        "quantity": 5,
                        "average_cost": 150,
                        "market_price": 175,
                        "market_value": 875,
                        "unrealized_pnl": 125,
                    }
                ],
                market_snapshots=[{"symbol": "NVDA", "time": observed_at, "close": 175, "change": 1.2}],
            )
        )
        assert result["status"] == "ok"

        ingestion = IngestionRepository(runtime)
        ingestion.register_source("option-test", name="Options", family="test", kind="option_chain")
        ingest_run = ingestion.start_run("option-test", "option_quotes")
        ingestion.store_option_snapshot(
            ingest_run,
            source_id="option-test",
            observed_at=observed_at,
                market_session="regular",
            universe="test",
            rows=[
                {
                    "symbol": "NVDA", "expiration": "2027-01-15", "strike": 200,
                    "option_type": "call", "underlying_price": 175, "bid": 4.8,
                    "ask": 5.2, "mid": 5, "volume": 120, "open_interest": 1500,
                    "iv": .41, "delta": .43,
                }
            ],
        )
        ingestion.finish_run(ingest_run, "succeeded")
        refresh_options_radar(runtime, source_id="option-test", code_version="broker-test")

        recommendations = repository.build_recommendations(code_version="broker-test")
        assert len(recommendations) == 1
        assert recommendations[0]["symbol"] == "NVDA"
        order = repository.stage_paper_order(recommendations[0]["recommendation_id"])
        assert order["status"] == "blocked"
        assert "insufficient_empirical_history" in recommendations[0]["blockers"]

        tables, _metadata = load_postgres_tables(
            {"database": {"url": postgres_dsn}},
            ("broker_status", "broker_accounts", "broker_positions", "agent_recommendations", "paper_orders"),
        )
    finally:
        runtime.close()

    assert tables["broker_status"][0]["account_mode"] == "paper"
    assert tables["broker_accounts"][0]["account_id"] == "DU123"
    assert tables["broker_positions"][0]["symbol"] == "NVDA"
    assert tables["agent_recommendations"][0]["symbol"] == "NVDA"
    assert tables["paper_orders"][0]["status"] == "blocked"
    with closing(psycopg.connect(postgres_dsn)) as connection:
        assert connection.execute("SELECT count(*) FROM raw.broker_position_snapshot").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM app.paper_order").fetchone()[0] == 1
