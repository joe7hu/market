from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.data_access.loaders import load_panel_scope_data
from investment_panel.core.decision import MARKET_DIMENSIONS, MARKET_HORIZONS, MarketStateSnapshot
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.confirmed_daily_prices import completed_trading_dates
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.market_analysis import refresh_market_publication
from investment_panel.database.runtime import DatabaseRuntime
from conftest import typed_config


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

        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        result = refresh_market_publication(runtime, now=cutoff)
        assert result["assets"] == 2
        assert result["drivers"] == 4
        assert result["coverage_rows"] == 48
        assert result["available_coverage_rows"] == 0
        repository = AnalysisRepository(runtime)
        assert {row["symbol"] for row in repository.publication_rows("market", "market_environment_assets")} == {"SPY", "QQQ"}
        assert {row["category"] for row in repository.publication_rows("market", "market_environment_model")} == {
            "Valuation", "Price Trend", "Market Breadth", "Risk Appetite"
        }

        panel = load_panel_scope_data(typed_config(migrated_postgres_dsn), "market")
        assert panel.status.ready is True
        assert len(panel.rows("market_environment_assets")) == 2
        assert len(panel.rows("market_environment_model")) == 4
        complete = load_panel_scope_data(typed_config(migrated_postgres_dsn), "dashboard")
        assert complete.status.ready is True
        assert complete.metadata["unavailable_models"] == []
        assert {row["symbol"] for row in complete.rows("technicals")} == {"SPY", "QQQ"}
        assert len(complete.rows("correlations")) == 1

        snapshot = MarketStateSnapshot.model_validate(
            repository.publication_rows("market", "market_state_snapshot")[0]
        )
        assert len(snapshot.coverage_matrix.rows) == 48
        assert {
            f"{row.horizon}:{row.dimension}:{row.asset_class}"
            for row in snapshot.coverage_matrix.rows
        } == {
            f"{horizon}:{dimension}:cross-asset"
            for horizon in MARKET_HORIZONS
            for dimension in MARKET_DIMENSIONS
        }
        assert all(
            row.current_status == "unavailable"
            for row in snapshot.coverage_matrix.rows
        )
        assert all(
            dimension.evidence_status == "unavailable"
            for dimensions in snapshot.horizons.values()
            for dimension in dimensions
        )
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
                (datetime(2025, 8, 1, tzinfo=UTC).date(), 100),
                (datetime(2025, 12, 31, tzinfo=UTC).date(), 120),
                (datetime(2026, 1, 2, tzinfo=UTC).date(), 121),
                (datetime(2026, 7, 1, tzinfo=UTC).date(), 132),
            )
        ]
        ingestion.store_price_bars(run_id, "ytd-test", rows)
        ingestion.finish_run(run_id, "succeeded")
        ingestion.register_source("ytd-override", name="YTD override", family="test", kind="quote", origin="test")
        override_run = ingestion.start_run("ytd-override", "quotes")
        ingestion.store_price_bars(
            override_run,
            "ytd-override",
            [{"symbol": "SPY", "date": datetime(2026, 7, 1, tzinfo=UTC).date(),
              "open": 140, "high": 140, "low": 140, "close": 140, "volume": 1}],
        )
        ingestion.finish_run(override_run, "succeeded")

        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        refresh_market_publication(runtime, now=cutoff)
        asset = AnalysisRepository(runtime).publication_rows("market", "market_environment_assets")[0]
        assert asset["return_ytd"] == pytest.approx(16.6666667)
        assert asset["return_1d"] == pytest.approx((140 / 121 - 1) * 100)
        assert asset["return_1y"] == pytest.approx(40.0)
        assert asset["source"] == "ytd-override"
    finally:
        runtime.close()


def test_market_coverage_uses_exact_history_per_horizon(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source("market-horizon-test", name="Market horizon test", family="test", kind="quote")
        run_id = ingestion.start_run("market-horizon-test", "quotes")
        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        dates = completed_trading_dates(cutoff, count=252)
        rows = [
            {
                "symbol": symbol,
                "date": trading_date,
                "open": base + index,
                "high": base + index,
                "low": base + index,
                "close": base + index,
                "volume": 1,
            }
            for symbol, base in (("SPY", 500), ("QQQ", 450), ("NVDA", 400))
            for index, trading_date in enumerate(reversed(dates))
        ]
        ingestion.store_price_bars(
            run_id,
            "market-horizon-test",
            rows,
            asset_classes={"SPY": "etf", "QQQ": "etf", "NVDA": "equity"},
        )
        ingestion.finish_run(run_id, "succeeded", item_count=len(rows), instrument_count=3)

        result = refresh_market_publication(runtime, now=cutoff)
        assert result["available_coverage_rows"] == 3
        repository = AnalysisRepository(runtime)
        consumed = repository.publication_at_or_before("market", cutoff=cutoff)
        assert consumed is not None
        assert consumed["publication_id"] == result["publication_id"]
        assert datetime.fromisoformat(consumed["input_cutoff"]).astimezone(UTC) == cutoff
        assert datetime.fromisoformat(consumed["published_at"]).astimezone(UTC) == cutoff
        snapshot = MarketStateSnapshot.model_validate(
            repository.publication_rows("market", "market_state_snapshot")[0]
        )

        states = {
            horizon: next(
                dimension for dimension in dimensions
                if dimension.dimension == "equity internals"
            )
            for horizon, dimensions in snapshot.horizons.items()
        }
        assert states["intraday"].evidence_status == "unavailable"
        assert states["intraday"].blockers == ("intraday_evidence_unavailable_from_daily_bars",)
        assert states["1-5 trading days"].evidence_status == "available"
        assert states["2-8 weeks"].evidence_status == "available"
        assert states["3-12 months"].evidence_status == "available"
        assert len(states["1-5 trading days"].lineage) == 15
        assert len(states["2-8 weeks"].lineage) == 120
        assert len(states["3-12 months"].lineage) == 756
        assert states["1-5 trading days"].history_start != states["2-8 weeks"].history_start
        assert states["2-8 weeks"].history_start != states["3-12 months"].history_start
        assert all(item.field == "market_daily_price" for item in snapshot.input_lineage)
        assert all(item.revision and item.fact_id for item in snapshot.input_lineage)

        microstructure = [
            next(dimension for dimension in dimensions if dimension.dimension == "microstructure")
            for dimensions in snapshot.horizons.values()
        ]
        assert all(
            dimension.evidence_status == "unavailable"
            and dimension.blockers == ("microstructure_execution_evidence_unavailable",)
            and not dimension.lineage
            for dimension in microstructure
        )
        valuation = repository.publication_rows("market", "market_environment_model")
        assert next(row for row in valuation if row["category"] == "Valuation")["score"] is None
    finally:
        runtime.close()
