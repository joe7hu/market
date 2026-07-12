from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app import data_access
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.runtime import DatabaseRuntime


# Regression: ISSUE-003 — Watchlist option IV, expected move, and skew were empty.
# Found by /qa on 2026-07-12
# Report: .gstack/qa-reports/qa-report-127-0-0-1-2026-07-12.md
def test_current_chain_is_composed_into_complete_watchlist_option_context(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    repository.register_source(
        "qa-options",
        name="QA options",
        family="market_data",
        kind="option_chain",
        capabilities={"option_quotes": True},
    )
    observed_at = datetime.now(UTC).replace(microsecond=0)
    expiry = (date.today() + timedelta(days=30)).isoformat()
    rows = [
        {
            "symbol": "CHAIN",
            "expiry": expiry,
            "strike": 100,
            "type": "call",
            "contract_symbol": "CHAIN-100C",
            "underlying_price": 100,
            "bid": 4.8,
            "ask": 5.2,
            "mid": 5.0,
            "iv": 0.35,
            "delta": 0.5,
        },
        {
            "symbol": "CHAIN",
            "expiry": expiry,
            "strike": 100,
            "type": "put",
            "contract_symbol": "CHAIN-100P",
            "underlying_price": 100,
            "bid": 4.6,
            "ask": 5.0,
            "mid": 4.8,
            "iv": 0.37,
            "delta": -0.5,
        },
        {
            "symbol": "CHAIN",
            "expiry": expiry,
            "strike": 110,
            "type": "call",
            "contract_symbol": "CHAIN-110C",
            "underlying_price": 100,
            "bid": 1.8,
            "ask": 2.2,
            "mid": 2.0,
            "iv": 0.3,
            "delta": 0.25,
        },
        {
            "symbol": "CHAIN",
            "expiry": expiry,
            "strike": 90,
            "type": "put",
            "contract_symbol": "CHAIN-90P",
            "underlying_price": 100,
            "bid": 2.8,
            "ask": 3.2,
            "mid": 3.0,
            "iv": 0.4,
            "delta": -0.25,
        },
    ]
    run_id = repository.start_run("qa-options", "option_quotes")
    try:
        repository.store_option_snapshot(
            run_id,
            source_id="qa-options",
            observed_at=observed_at,
            market_session="closed",
            universe="watchlist",
            rows=rows,
            completeness=1.0,
        )
        repository.finish_run(run_id, "succeeded")

        panel = data_access.load_table_panel_data(
            {"database": {"url": migrated_postgres_dsn}}, "options_ticker_signals"
        )
        row = next(
            item for item in panel.rows("options_ticker_signals") if item["symbol"] == "CHAIN"
        )

        assert row["status"] == "loaded"
        assert row["iv_regime"] == "normal"
        assert row["expected_move_pct"] == 0.098
        assert row["put_call_iv_skew"] == pytest.approx(0.1)
        assert row["skew_signal"] == "put premium"
        assert row["spread_quality"] == "usable"
    finally:
        runtime.close()
