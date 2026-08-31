from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from math import log, sqrt
from statistics import mean, pstdev

import pytest

from app.data_access.loaders import load_panel_scope_data
from investment_panel.core.decision import MARKET_DIMENSIONS, MARKET_HORIZONS, MarketStateSnapshot, is_us_market_day
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.confirmed_daily_prices import completed_trading_dates
from investment_panel.database.ingestion import IngestionRepository
import investment_panel.database.market_analysis as market_analysis
from investment_panel.database.market_analysis import refresh_market_publication
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.source_facts import SourceFactRepository
from conftest import typed_config


def _seed_crypto_bars(
    runtime: DatabaseRuntime,
    *,
    dates: tuple,
    source_id: str = "daily-market-prices",
    symbols: tuple[str, ...] = ("BTC-USD", "ETH-USD", "SOL-USD"),
    currency: str = "USD",
    volume: float = 10,
    finish_status: str | None = "succeeded",
) -> None:
    ingestion = IngestionRepository(runtime)
    ingestion.register_source(
        source_id,
        name=source_id,
        family="market_data",
        kind="daily_bars",
        origin="test" if source_id != "daily-market-prices" else None,
    )
    run_id = ingestion.start_run(source_id, "price_bars")
    rows = [
        {
            "symbol": symbol,
            "date": trading_date,
            "open": 1,
            "high": 1,
            "low": 1,
            "close": 1,
            "volume": volume + index,
            "currency": currency,
        }
        for symbol in symbols
        for index, trading_date in enumerate(dates)
    ]
    ingestion.store_price_bars(
        run_id,
        source_id,
        rows,
        asset_classes={symbol: "crypto" for symbol in symbols},
    )
    if finish_status is not None:
        ingestion.finish_run(run_id, finish_status)


def _crypto_states(snapshot: MarketStateSnapshot) -> dict[str, object]:
    return {
        horizon: next(item for item in dimensions if item.dimension == "crypto liquidity")
        for horizon, dimensions in snapshot.horizons.items()
    }


def _assert_crypto_unavailable(
    snapshot: MarketStateSnapshot,
    blocker: str,
    *,
    count_key: str | None = None,
    count: int = 0,
) -> None:
    states = _crypto_states(snapshot)
    for horizon in MARKET_HORIZONS[1:]:
        state = states[horizon]
        assert state.evidence_status == "unavailable"
        assert state.blockers == (blocker,)
        assert state.latest_aggregate_volume_usd is None
        assert state.median_aggregate_daily_volume_usd is None
        assert state.latest_to_horizon_median_ratio is None
        assert not state.lineage
        if count_key is not None:
            assert state.model_dump(mode="json")[count_key] == count
        coverage = next(
            row for row in snapshot.coverage_matrix.rows
            if row.dimension == "crypto liquidity" and row.horizon == horizon
        )
        assert coverage.current_status == "unavailable"
        assert not coverage.input_lineage
        if count_key is not None:
            assert coverage.model_dump(mode="json")[count_key] == count


def _backdate_source(connection, source_id: str, cutoff: datetime) -> None:
    visible_at = cutoff - timedelta(days=1)
    connection.execute(
        "UPDATE ingest.source SET created_at = %s WHERE id = %s",
        [visible_at, source_id],
    )


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


def test_market_publication_uses_the_exact_refreshed_equity_benchmark(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES "
                "('IN-SCOPE', 'In scope', 'equity'), ('OUT-SCOPE', 'Out of scope', 'equity')"
            )

        refresh_market_publication(
            runtime,
            now=datetime.now(UTC),
            benchmark_symbols=["IN-SCOPE"],
            configured_watchlist=[{"symbol": "OUT-SCOPE"}],
        )

        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
        price_state = next(
            row
            for row in snapshot.coverage_matrix.rows
            if row.horizon == "1-5 trading days" and row.dimension == "equity internals"
        )
        corporate_state = next(
            row
            for row in snapshot.coverage_matrix.rows
            if row.horizon == "3-12 months" and row.dimension == "corporate cycle"
        )
        assert price_state.eligible_member_count == 1
        assert price_state.missing_member_count == 1
        assert corporate_state.eligible_member_count == 1

        with runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO app.watchlist_item (instrument_id, watch_state) "
                "SELECT id, 'excluded' FROM catalog.instrument WHERE symbol = 'OUT-SCOPE' "
                "ON CONFLICT (instrument_id) DO UPDATE SET watch_state = EXCLUDED.watch_state, "
                "updated_at = EXCLUDED.updated_at"
            )
        refresh_market_publication(
            runtime,
            now=datetime.now(UTC),
            configured_watchlist=[{"symbol": "OUT-SCOPE"}],
        )
        unscoped = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
        unscoped_corporate = next(
            row for row in unscoped.horizons["3-12 months"]
            if row.dimension == "corporate cycle"
        )
        assert unscoped_corporate.eligible_member_count == 0
        assert unscoped_corporate.eligible_members == []
    finally:
        runtime.close()


def test_market_crypto_volume_uses_fixed_utc_windows_and_lineage(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        source_id = "daily-market-prices"
        ingestion.register_source(source_id, name="Daily market prices", family="market_data", kind="daily_bars")
        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        dates = market_analysis._completed_crypto_dates(cutoff, count=252)
        run_id = ingestion.start_run(source_id, "price_bars")
        rows = [
            {
                "symbol": symbol,
                "date": trading_date,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": base + index,
                "currency": "USD",
            }
            for symbol, base in (("BTC-USD", 100), ("ETH-USD", 200), ("SOL-USD", 300))
            for index, trading_date in enumerate(dates)
        ]
        assert ingestion.store_price_bars(
            run_id, source_id, rows,
            asset_classes={symbol: "crypto" for symbol in ("BTC-USD", "ETH-USD", "SOL-USD")},
        ) == len(rows)
        ingestion.finish_run(run_id, "succeeded", item_count=len(rows), instrument_count=3)

        result = refresh_market_publication(runtime, now=cutoff)
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
        states = {
            horizon: next(item for item in dimensions if item.dimension == "crypto liquidity")
            for horizon, dimensions in snapshot.horizons.items()
        }
        assert [states[horizon].evidence_status for horizon in MARKET_HORIZONS] == [
            "unavailable", "available", "available", "available"
        ]
        assert states["intraday"].blockers == ("crypto_liquidity_daily_volume_unsupported_for_intraday",)
        assert states["intraday"].data_requests == ["intraday_crypto_spread_depth_execution_data"]
        assert [states[horizon].expected_calendar_days for horizon in MARKET_HORIZONS] == [0, 5, 40, 252]
        assert [len(states[horizon].lineage) for horizon in MARKET_HORIZONS] == [0, 15, 120, 756]
        state = states["1-5 trading days"]
        assert state.state == "reported daily USD crypto trading volume"
        assert state.benchmark_key == "market-crypto-majors"
        assert tuple(state.eligible_members) == ("BTC-USD", "ETH-USD", "SOL-USD")
        assert state.latest_aggregate_volume_usd == pytest.approx(600)
        assert state.median_aggregate_daily_volume_usd == pytest.approx(606)
        assert state.latest_to_horizon_median_ratio == pytest.approx(600 / 606)
        assert state.window_start == dates[4].isoformat()
        assert state.window_end == dates[0].isoformat()
        assert all(item.field == "crypto_daily_trading_volume_usd" for item in state.lineage)
        assert all(item.available_at <= cutoff and item.currency == "USD" for item in state.lineage)
        assert all("three-of-three denominator" in driver for driver in state.change_drivers[:1])
        assert result["coverage_rows"] == 48
        crypto_coverage = [
            row for row in snapshot.coverage_matrix.rows if row.dimension == "crypto liquidity"
        ]
        assert [row.current_status for row in crypto_coverage] == ["unavailable", "available", "available", "available"]
        assert [len(row.input_lineage) for row in crypto_coverage] == [0, 15, 120, 756]
        assert all(row.decision_impact == "context" for row in crypto_coverage)

        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE raw.price_bar SET volume = volume + 1 WHERE source_id = %s AND trading_date = %s",
                [source_id, dates[0]],
            )
        changed = refresh_market_publication(runtime, now=cutoff)
        assert changed["snapshot_id"] != result["snapshot_id"]
    finally:
        runtime.close()


def test_market_crypto_volume_fails_closed_for_wrong_currency(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        source_id = "daily-market-prices"
        ingestion.register_source(source_id, name="Daily market prices", family="market_data", kind="daily_bars")
        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        dates = market_analysis._completed_crypto_dates(cutoff, count=5)
        run_id = ingestion.start_run(source_id, "price_bars")
        rows = [
            {
                "symbol": symbol, "date": trading_date, "open": 1, "high": 1, "low": 1,
                "close": 1, "volume": 10, "currency": "EUR",
            }
            for symbol in ("BTC-USD", "ETH-USD", "SOL-USD")
            for trading_date in dates
        ]
        ingestion.store_price_bars(
            run_id, source_id, rows,
            asset_classes={symbol: "crypto" for symbol in ("BTC-USD", "ETH-USD", "SOL-USD")},
        )
        ingestion.finish_run(run_id, "succeeded")
        refresh_market_publication(runtime, now=cutoff)
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
        state = next(item for item in snapshot.horizons["1-5 trading days"] if item.dimension == "crypto liquidity")
        coverage = next(
            row for row in snapshot.coverage_matrix.rows
            if row.dimension == "crypto liquidity" and row.horizon == "1-5 trading days"
        )
        assert state.evidence_status == "unavailable"
        assert state.blockers == ("crypto_daily_trading_volume_wrong_currency",)
        assert state.wrong_currency_member_count == 3
        assert state.latest_aggregate_volume_usd is None
        assert state.median_aggregate_daily_volume_usd is None
        assert not state.lineage
        assert coverage.current_status == "unavailable"
        assert coverage.wrong_currency_member_count == 3
        assert not coverage.input_lineage
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("quality", "expected_blocker", "count_key"),
    [
        ("missing", "crypto_daily_trading_volume_missing", "missing_member_count"),
        ("truncated", "crypto_daily_trading_volume_truncated", "truncated_member_count"),
        ("duplicate", "crypto_daily_trading_volume_duplicate", "duplicate_member_count"),
        ("invalid", "crypto_daily_trading_volume_invalid", "invalid_member_count"),
        ("future", "crypto_daily_trading_volume_truncated", "truncated_member_count"),
        ("late_available", "crypto_daily_trading_volume_truncated", "truncated_member_count"),
    ],
)
def test_market_crypto_volume_fails_closed_for_postgres_quality(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    quality: str,
    expected_blocker: str,
    count_key: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        dates = market_analysis._completed_crypto_dates(cutoff, count=252)
        symbols = ("BTC-USD", "ETH-USD") if quality == "missing" else ("BTC-USD", "ETH-USD", "SOL-USD")
        _seed_crypto_bars(runtime, dates=dates, symbols=symbols)
        with runtime.read() as connection:
            instrument_id = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = 'SOL-USD'"
            ).fetchone()
            instrument_id = instrument_id["id"] if instrument_id else None
        if quality != "missing":
            assert instrument_id is not None
        if quality == "truncated":
            with runtime.transaction() as connection:
                connection.execute(
                    "UPDATE raw.price_bar SET trading_date = %s "
                    "WHERE instrument_id = %s AND trading_date = %s",
                    [dates[0] - timedelta(days=1), instrument_id, dates[0]],
                )
        elif quality == "invalid":
            with runtime.transaction() as connection:
                connection.execute(
                    "UPDATE raw.price_bar SET volume = 0 "
                    "WHERE instrument_id = %s AND trading_date = %s",
                    [instrument_id, dates[0]],
                )
        elif quality == "future":
            with runtime.transaction() as connection:
                connection.execute(
                    "UPDATE raw.price_bar SET observed_at = %s "
                    "WHERE instrument_id = %s AND trading_date = %s",
                    [cutoff + timedelta(minutes=1), instrument_id, dates[0]],
                )
        elif quality == "late_available":
            with runtime.transaction() as connection:
                fact = connection.execute(
                    "UPDATE raw.price_bar SET available_at = %s "
                    "WHERE instrument_id = %s AND trading_date = %s "
                    "RETURNING id",
                    [cutoff + timedelta(minutes=1), instrument_id, dates[0]],
                ).fetchone()
                connection.execute(
                    "UPDATE raw.price_bar_fact_availability SET fact_available_at = %s "
                    "WHERE fact_id = %s",
                    [cutoff + timedelta(minutes=1), fact["id"]],
                )
        else:
            original_confirmed_daily_bars = market_analysis.confirmed_daily_bars

            def confirmed_daily_bars_with_duplicate(*args, **kwargs):
                selected = original_confirmed_daily_bars(*args, **kwargs)
                duplicated = {instrument: list(rows) for instrument, rows in selected.items()}
                duplicated[instrument_id].append(dict(duplicated[instrument_id][-1]))
                return duplicated

            if quality == "duplicate":
                monkeypatch.setattr(market_analysis, "confirmed_daily_bars", confirmed_daily_bars_with_duplicate)
        refresh_market_publication(runtime, now=cutoff)
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
    finally:
        runtime.close()
    _assert_crypto_unavailable(snapshot, expected_blocker, count_key=count_key, count=1)


@pytest.mark.parametrize(
    ("source_case", "expected_blocker", "count_key"),
    [
        ("disabled", "crypto_daily_trading_volume_source_lifecycle_mismatch", "invalid_member_count"),
        ("inactive", "crypto_daily_trading_volume_source_lifecycle_mismatch", "invalid_member_count"),
        ("cadence", "crypto_daily_trading_volume_source_lifecycle_mismatch", "invalid_member_count"),
        ("wrong_owner", "crypto_daily_trading_volume_source_lifecycle_mismatch", "invalid_member_count"),
        ("failed", "crypto_daily_trading_volume_source_run_unavailable", "invalid_member_count"),
        ("unfinished", "crypto_daily_trading_volume_source_run_unavailable", "invalid_member_count"),
        ("future_finished", "crypto_daily_trading_volume_source_run_unavailable", "invalid_member_count"),
        ("stale", "crypto_daily_trading_volume_source_run_stale", "stale_member_count"),
    ],
)
def test_market_crypto_volume_fails_closed_for_source_lifecycle_and_run(
    migrated_postgres_dsn: str,
    source_case: str,
    expected_blocker: str,
    count_key: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        dates = market_analysis._completed_crypto_dates(cutoff, count=252)
        status = "failed" if source_case == "failed" else None if source_case == "unfinished" else "succeeded"
        _seed_crypto_bars(runtime, dates=dates, finish_status=status)
        ingestion = IngestionRepository(runtime)
        if source_case == "disabled":
            ingestion.set_source_enabled("daily-market-prices", False)
        elif source_case == "inactive":
            with runtime.transaction() as connection:
                connection.execute(
                    "UPDATE ingest.source SET operational_state = 'standby' WHERE id = %s",
                    ["daily-market-prices"],
                )
        elif source_case == "cadence":
            with runtime.transaction() as connection:
                connection.execute(
                    "UPDATE ingest.source SET freshness_seconds = 86400 WHERE id = %s",
                    ["daily-market-prices"],
                )
        elif source_case == "wrong_owner":
            with runtime.transaction() as connection:
                connection.execute(
                    "UPDATE ingest.source SET health_owner = 'wrong-owner' WHERE id = %s",
                    ["daily-market-prices"],
                )
        elif source_case == "future_finished":
            with runtime.transaction() as connection:
                connection.execute(
                    "UPDATE ingest.run SET finished_at = %s "
                    "WHERE source_id = %s AND capability = 'price_bars'",
                    [cutoff + timedelta(minutes=1), "daily-market-prices"],
                )
        elif source_case == "stale":
            with runtime.transaction() as connection:
                connection.execute(
                    "UPDATE ingest.run SET finished_at = %s "
                    "WHERE source_id = %s AND capability = 'price_bars'",
                    [cutoff - timedelta(hours=2), "daily-market-prices"],
                )
        refresh_market_publication(runtime, now=cutoff)
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
    finally:
        runtime.close()
    _assert_crypto_unavailable(snapshot, expected_blocker, count_key=count_key, count=3)


def test_market_crypto_volume_fails_closed_for_missing_daily_market_prices_source(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        dates = market_analysis._completed_crypto_dates(cutoff, count=252)
        _seed_crypto_bars(runtime, dates=dates, source_id="polygon")
        refresh_market_publication(runtime, now=cutoff)
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
    finally:
        runtime.close()
    _assert_crypto_unavailable(
        snapshot,
        "crypto_daily_trading_volume_source_unavailable",
        count_key="invalid_member_count",
        count=3,
    )


def test_market_crypto_volume_binds_freshness_to_selected_daily_facts(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        dates = market_analysis._completed_crypto_dates(cutoff, count=252)
        _seed_crypto_bars(runtime, dates=dates)
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET finished_at = %s "
                "WHERE source_id = %s AND capability = 'price_bars'",
                [cutoff - timedelta(hours=2), "daily-market-prices"],
            )
        _seed_crypto_bars(runtime, dates=(cutoff.date(),))
        refresh_market_publication(runtime, now=cutoff)
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
    finally:
        runtime.close()
    _assert_crypto_unavailable(
        snapshot,
        "crypto_daily_trading_volume_stale",
        count_key="stale_member_count",
        count=3,
    )


def test_market_crypto_volume_excludes_current_day_partial(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        dates = market_analysis._completed_crypto_dates(cutoff, count=5)
        _seed_crypto_bars(runtime, dates=(*dates, cutoff.date()))
        refresh_market_publication(runtime, now=cutoff)
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
    finally:
        runtime.close()
    state = _crypto_states(snapshot)["1-5 trading days"]
    assert state.evidence_status == "available"
    assert state.window_end == dates[0].isoformat()
    assert all(item.trading_date != cutoff.date() for item in state.lineage)
    assert len(state.lineage) == 15


def test_market_crypto_volume_rejects_noncanonical_source_collision(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        dates = market_analysis._completed_crypto_dates(cutoff, count=252)
        _seed_crypto_bars(runtime, dates=dates)
        _seed_crypto_bars(runtime, dates=dates, source_id="polygon", volume=1_000_000)
        refresh_market_publication(runtime, now=cutoff)
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
    finally:
        runtime.close()
    _assert_crypto_unavailable(
        snapshot,
        "crypto_daily_trading_volume_wrong_source",
        count_key="wrong_source_member_count",
        count=3,
    )


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
        dates = completed_trading_dates(datetime.now(UTC), count=252)
        rows = [
            {
                "symbol": symbol,
                "date": trading_date,
                "open": base + index,
                "high": base + index,
                "low": base + index,
                "close": base + index,
                "volume": 1,
                "is_complete": True,
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
        cutoff = datetime.now(UTC)

        result = refresh_market_publication(
            runtime, now=cutoff, benchmark_symbols=["SPY", "QQQ", "NVDA"]
        )
        assert result["available_coverage_rows"] == 5
        repository = AnalysisRepository(runtime)
        consumed = repository.publication_by_id("market", result["publication_id"])
        assert consumed is not None
        assert consumed["publication_id"] == result["publication_id"]
        assert datetime.fromisoformat(consumed["input_cutoff"]).astimezone(UTC) == cutoff
        assert datetime.fromisoformat(consumed["published_at"]).astimezone(UTC) > cutoff
        snapshot = MarketStateSnapshot.model_validate(
            repository.publication_rows("market", "market_state_snapshot")[0]
        )
        assert snapshot.publication_id is None

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

        volatility_states = {
            horizon: next(
                dimension for dimension in dimensions if dimension.dimension == "volatility"
            )
            for horizon, dimensions in snapshot.horizons.items()
        }
        assert volatility_states["1-5 trading days"].evidence_status == "available"
        assert volatility_states["2-8 weeks"].evidence_status == "available"
        assert volatility_states["3-12 months"].evidence_status == "unavailable"
        assert volatility_states["3-12 months"].blockers == ("market_daily_history_truncated",)
        assert volatility_states["1-5 trading days"].model_dump(mode="json")["return_window_trading_days"] == 5
        assert volatility_states["2-8 weeks"].model_dump(mode="json")["return_window_trading_days"] == 40
        assert len(volatility_states["1-5 trading days"].lineage) == 18
        assert len(volatility_states["2-8 weeks"].lineage) == 123

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


def test_market_realized_volatility_uses_exact_extra_close_lineage_and_identity(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        source_id = "market-volatility-test"
        ingestion.register_source(source_id, name="Market volatility test", family="test", kind="quote")
        run_id = ingestion.start_run(source_id, "quotes")
        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        dates = completed_trading_dates(cutoff, count=253)
        rows = [
            {
                "symbol": symbol,
                "date": trading_date,
                "open": base + index,
                "high": base + index,
                "low": base + index,
                "close": base + index,
                "volume": 1,
                "is_complete": True,
            }
            for symbol, base in (("SPY", 500), ("QQQ", 450), ("NVDA", 400))
            for index, trading_date in enumerate(reversed(dates))
        ]
        ingestion.store_price_bars(
            run_id, source_id, rows,
            asset_classes={"SPY": "etf", "QQQ": "etf", "NVDA": "equity"},
        )
        ingestion.finish_run(run_id, "succeeded", item_count=len(rows), instrument_count=3)

        result = refresh_market_publication(
            runtime, now=cutoff, benchmark_symbols=["SPY", "QQQ", "NVDA"]
        )
        repository = AnalysisRepository(runtime)
        snapshot = MarketStateSnapshot.model_validate(
            repository.publication_rows("market", "market_state_snapshot")[0]
        )
        volatility_states = {
            horizon: next(
                dimension for dimension in dimensions if dimension.dimension == "volatility"
            )
            for horizon, dimensions in snapshot.horizons.items()
        }
        assert result["available_coverage_rows"] == 6, [
            state.model_dump(mode="json") for state in volatility_states.values()
        ]
        assert volatility_states["intraday"].evidence_status == "unavailable"
        daily_volatility_states = [
            volatility_states[horizon]
            for horizon in ("1-5 trading days", "2-8 weeks", "3-12 months")
        ]
        assert all(state.evidence_status == "available" for state in daily_volatility_states)
        assert all(state.state == "realized historical volatility" for state in daily_volatility_states)
        assert all(state.probability is None for state in daily_volatility_states)
        assert all("not implied volatility" in state.change_drivers[1] for state in daily_volatility_states)
        assert [len(volatility_states[horizon].lineage) for horizon in (
            "1-5 trading days", "2-8 weeks", "3-12 months"
        )] == [18, 123, 759]
        assert len(snapshot.input_lineage) == 759
        assert all(item.available_at <= cutoff for item in snapshot.input_lineage)

        expected = {}
        for base, horizon, period in (
            (500, "1-5 trading days", 5),
            (450, "1-5 trading days", 5),
            (400, "1-5 trading days", 5),
        ):
            closes = [base + 252 - index for index in range(6)]
            expected.setdefault(horizon, []).append(pstdev(
                [log(current / previous) for previous, current in zip(closes, closes[1:], strict=False)]
            ) * sqrt(252))
        state = volatility_states["1-5 trading days"].model_dump(mode="json")
        assert state["realized_volatility"] == pytest.approx(mean(expected["1-5 trading days"]))
        assert state["eligible_member_count"] == state["available_member_count"] == 3
        assert state["missing_member_count"] == state["stale_member_count"] == 0
        assert state["truncated_member_count"] == 0
        assert state["required_history_trading_days"] == 6

        coverage = [
            row for row in snapshot.coverage_matrix.rows if row.dimension == "volatility"
        ]
        assert len(coverage) == 4
        assert coverage[0].current_status == "unavailable"
        assert all(row.current_status == "available" for row in coverage[1:])
        assert all(row.decision_impact == "context" and row.point_in_time_safe for row in coverage[1:])
        assert [row.required_history_trading_days for row in coverage] == [0, 6, 41, 253]
        assert sorted({json.dumps(item.model_dump(mode="json"), sort_keys=True) for item in snapshot.input_lineage}) == sorted(
            {json.dumps(item.model_dump(mode="json"), sort_keys=True)
             for row in coverage
             for item in row.input_lineage}
        )

        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE raw.price_bar SET close = close + 1 WHERE trading_date = %s",
                [dates[-1]],
            )
        changed_result = refresh_market_publication(
            runtime, now=cutoff, benchmark_symbols=["SPY", "QQQ", "NVDA"]
        )
        assert changed_result["snapshot_id"] != result["snapshot_id"]
        changed_snapshot = AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        assert changed_snapshot["snapshot_id"] == changed_result["snapshot_id"]
    finally:
        runtime.close()


def test_market_realized_volatility_stays_unavailable_for_incomplete_member(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        source_id = "market-volatility-quality-test"
        ingestion.register_source(source_id, name="Market volatility quality test", family="test", kind="quote")
        run_id = ingestion.start_run(source_id, "quotes")
        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        dates = completed_trading_dates(cutoff, count=253)
        rows = [
            {
                "symbol": symbol,
                "date": trading_date,
                "open": base + index,
                "high": base + index,
                "low": base + index,
                "close": base + index,
                "volume": 1,
                "is_complete": True,
            }
            for symbol, base in (("SPY", 500), ("QQQ", 450), ("NVDA", 400))
            for index, trading_date in enumerate(reversed(dates))
        ]
        ingestion.store_price_bars(
            run_id, source_id, rows,
            asset_classes={"SPY": "etf", "QQQ": "etf", "NVDA": "equity"},
        )
        ingestion.finish_run(run_id, "succeeded", item_count=len(rows), instrument_count=3)
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE raw.price_bar SET close = 0 WHERE trading_date = %s AND source_id = %s",
                [dates[0], source_id],
            )

        refresh_market_publication(
            runtime, now=cutoff, benchmark_symbols=["SPY", "QQQ", "NVDA"]
        )
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
        states = [
            dimension
            for dimensions in snapshot.horizons.values()
            for dimension in dimensions
            if dimension.dimension == "volatility"
        ]
        assert all(state.evidence_status == "unavailable" for state in states)
        assert states[0].blockers == ("intraday_evidence_unavailable_from_daily_bars",)
        assert all(state.blockers == ("market_daily_history_truncated",) for state in states[1:]), [
            state.model_dump(mode="json") for state in states
        ]
        assert all(not state.lineage for state in states)
        coverage = [row for row in snapshot.coverage_matrix.rows if row.dimension == "volatility"]
        assert all(row.current_status == "unavailable" for row in coverage)
        assert tuple(coverage[0].blockers) == ("intraday_evidence_unavailable_from_daily_bars",)
        assert all(tuple(row.blockers) == ("market_daily_history_truncated",) for row in coverage[1:])
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("quality", "expected_blocker", "count_key"),
    [
        ("missing", "market_daily_history_missing", "missing_member_count"),
        ("stale", "market_daily_history_stale", "stale_member_count"),
        ("duplicate", "market_daily_history_duplicate", "duplicate_member_count"),
        ("future", "market_daily_history_truncated", "truncated_member_count"),
    ],
)
def test_market_realized_volatility_fails_closed_per_member_quality(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    quality: str,
    expected_blocker: str,
    count_key: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        dates = completed_trading_dates(cutoff, count=253)
        good_source = f"market-quality-good-{quality}"
        bad_source = f"market-quality-bad-{quality}"
        for source_id in (good_source, bad_source):
            ingestion.register_source(
                source_id, name="Market quality test", family="test", kind="quote", origin="test"
            )
        for source_id, symbols in ((good_source, ("SPY", "QQQ")), (bad_source, ("NVDA",))):
            run_id = ingestion.start_run(source_id, "quotes")
            rows = [
                {
                    "symbol": symbol,
                    "date": trading_date,
                    "open": base + index,
                    "high": base + index,
                    "low": base + index,
                    "close": base + index,
                    "volume": 1,
                    "is_complete": True,
                }
                for symbol, base in (("SPY", 500), ("QQQ", 450), ("NVDA", 400))
                if symbol in symbols
                for index, trading_date in enumerate(reversed(dates))
            ]
            ingestion.store_price_bars(
                run_id, source_id, rows,
                asset_classes={symbol: "etf" for symbol in symbols},
            )
            ingestion.finish_run(run_id, "succeeded")

        with runtime.read() as connection:
            bad_instrument_id = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = 'NVDA'"
            ).fetchone()["id"]
        if quality == "missing":
            with runtime.transaction() as connection:
                connection.execute(
                    "UPDATE raw.price_bar SET close = 0 WHERE instrument_id = %s",
                    [bad_instrument_id],
                )
        elif quality == "stale":
            stale_at = cutoff - timedelta(days=8)
            with runtime.transaction() as connection:
                connection.execute(
                    """
                    UPDATE raw.price_bar bar
                    SET available_at = %s
                    WHERE bar.instrument_id = %s AND bar.source_id = %s
                      AND bar.trading_date = %s
                    """, [stale_at, bad_instrument_id, bad_source, dates[0]]
                )
                connection.execute(
                    """
                    UPDATE raw.price_bar_fact_availability availability
                    SET fact_available_at = %s
                    FROM raw.price_bar bar
                    WHERE availability.fact_id = bar.id
                      AND bar.instrument_id = %s AND bar.source_id = %s
                      AND bar.trading_date = %s
                    """, [stale_at, bad_instrument_id, bad_source, dates[0]]
                )
        elif quality == "future":
            with runtime.transaction() as connection:
                connection.execute(
                    """
                    UPDATE raw.price_bar
                    SET observed_at = %s
                    WHERE instrument_id = %s AND source_id = %s
                      AND trading_date = %s
                    """, [cutoff + timedelta(minutes=1), bad_instrument_id, bad_source, dates[0]]
                )
        elif quality == "disabled":
            ingestion.set_source_enabled(bad_source, False)
        elif quality == "duplicate":
            original_confirmed_daily_bars = market_analysis.confirmed_daily_bars

            def confirmed_daily_bars_with_duplicate(*args, **kwargs):
                selected = original_confirmed_daily_bars(*args, **kwargs)
                duplicated = {instrument_id: list(rows) for instrument_id, rows in selected.items()}
                duplicated[bad_instrument_id].append(dict(duplicated[bad_instrument_id][-1]))
                return duplicated

            monkeypatch.setattr(market_analysis, "confirmed_daily_bars", confirmed_daily_bars_with_duplicate)
        refresh_market_publication(
            runtime, now=cutoff, benchmark_symbols=["SPY", "QQQ", "NVDA"]
        )
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
    finally:
        runtime.close()

    other_count_keys = {
        "missing_member_count", "stale_member_count", "truncated_member_count",
        "duplicate_member_count", "invalid_member_count",
    } - {count_key}
    states = {
        horizon: next(dimension for dimension in dimensions if dimension.dimension == "volatility")
        for horizon, dimensions in snapshot.horizons.items()
    }
    coverage = {
        row.horizon: row for row in snapshot.coverage_matrix.rows if row.dimension == "volatility"
    }
    assert states["intraday"].evidence_status == "unavailable"
    for horizon in ("1-5 trading days", "2-8 weeks", "3-12 months"):
        state = states[horizon]
        assert state.evidence_status == "unavailable"
        assert state.blockers == (expected_blocker,)
        assert not state.lineage
        assert state.model_dump(mode="json")[count_key] == 1
        assert all(state.model_dump(mode="json")[key] == 0 for key in other_count_keys)
        row = coverage[horizon]
        assert row.current_status == "unavailable"
        assert tuple(row.blockers) == (expected_blocker,)
        assert not row.input_lineage
        assert row.model_dump(mode="json")[count_key] == 1


@pytest.mark.parametrize("source_state", ["disabled", "unconfirmed", "unfinished"])
def test_market_realized_volatility_fails_closed_for_unusable_postgres_source(
    migrated_postgres_dsn: str,
    source_state: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        dates = completed_trading_dates(cutoff, count=253)
        good_source = f"market-quality-good-{source_state}"
        bad_source = f"market-quality-bad-{source_state}"
        ingestion.register_source(
            good_source, name="Good quality test", family="test", kind="quote", origin="test"
        )
        ingestion.register_source(
            bad_source, name="Bad quality test", family="test", kind="quote", origin="test"
        )
        for source_id, symbols in ((good_source, ("SPY", "QQQ")), (bad_source, ("NVDA",))):
            run_id = ingestion.start_run(source_id, "quotes")
            rows = [
                {
                    "symbol": symbol,
                    "date": trading_date,
                    "open": base + index,
                    "high": base + index,
                    "low": base + index,
                    "close": base + index,
                    "volume": 1,
                    "is_complete": True,
                }
                for symbol, base in (("SPY", 500), ("QQQ", 450), ("NVDA", 400))
                if symbol in symbols
                for index, trading_date in enumerate(reversed(dates))
            ]
            ingestion.store_price_bars(
                run_id, source_id, rows,
                asset_classes={symbol: "etf" for symbol in symbols},
            )
            if source_id == bad_source:
                if source_state == "unfinished":
                    continue
                ingestion.finish_run(run_id, "failed" if source_state == "unconfirmed" else "succeeded")
            else:
                ingestion.finish_run(run_id, "succeeded")
        if source_state == "disabled":
            ingestion.set_source_enabled(bad_source, False)

        refresh_market_publication(
            runtime, now=cutoff, benchmark_symbols=["SPY", "QQQ", "NVDA"]
        )
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
        states = [
            dimension
            for dimensions in snapshot.horizons.values()
            for dimension in dimensions
            if dimension.dimension == "volatility"
        ]
        assert all(state.evidence_status == "unavailable" for state in states)
        assert states[0].blockers == ("intraday_evidence_unavailable_from_daily_bars",)
        assert all(state.blockers == ("market_daily_history_missing",) for state in states[1:])
        assert all(not state.lineage for state in states)
        assert all(state.model_dump(mode="json")["missing_member_count"] == 1 for state in states[1:])
        coverage = [row for row in snapshot.coverage_matrix.rows if row.dimension == "volatility"]
        assert all(row.current_status == "unavailable" for row in coverage)
        assert tuple(coverage[0].blockers) == ("intraday_evidence_unavailable_from_daily_bars",)
        assert all(tuple(row.blockers) == ("market_daily_history_missing",) for row in coverage[1:])
        assert all(not row.input_lineage for row in coverage)
        assert all(row.model_dump(mode="json")["missing_member_count"] == 1 for row in coverage[1:])
    finally:
        runtime.close()


def test_market_event_risk_uses_latest_cutoff_visible_versions(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    facts = SourceFactRepository(runtime)
    source_id = "official-event-calendar"
    cutoff = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)
    ingestion.register_source(
        source_id, name="Official events", family="events", kind="calendar", origin="test",
        operational_state="active", health_owner="update_event_calendar", freshness_seconds=86400,
    )

    def store(
        key: str, title: str, starts_at: datetime, kind: str, suffix: str, *, status: str | None = "succeeded"
    ) -> object:
        run_id = ingestion.start_run(source_id, "macro_events", source_run_key=suffix)
        assert facts.store_market_events(run_id, source_id, [{
            "source_key": key, "event_scope": "macro", "event_kind": kind, "title": title,
            "starts_at": starts_at, "verification_status": "scheduled",
        }]) == 1
        if status is not None:
            ingestion.finish_run(run_id, status)
        return run_id

    try:
        old_run = store("short", "Old CPI release", datetime(2026, 9, 1, 14, tzinfo=UTC), "inflation", "old")
        latest_run = store("short", "Latest CPI release", datetime(2026, 9, 1, 14, tzinfo=UTC), "inflation", "latest")
        future_run = store("short", "Future CPI release", datetime(2026, 9, 1, 14, tzinfo=UTC), "inflation", "future")
        intraday_run = store("intraday", "Intraday FOMC briefing", datetime(2026, 8, 28, 16, tzinfo=UTC), "central_bank", "intraday")
        weeks_run = store("weeks", "Weeks PPI release", datetime(2026, 10, 1, 14, tzinfo=UTC), "inflation", "weeks")
        long_run = store("long", "Long FOMC meeting", datetime(2027, 1, 15, 14, tzinfo=UTC), "central_bank", "long")
        beyond_run = store(
            "beyond", "Beyond long horizon", datetime(2027, 10, 1, 14, tzinfo=UTC), "inflation", "beyond",
        )
        unfinished_run = store(
            "unfinished", "Unfinished CPI release", datetime(2026, 9, 2, 14, tzinfo=UTC), "inflation", "unfinished",
            status=None,
        )
        failed_run = store(
            "failed", "Failed CPI release", datetime(2026, 9, 3, 14, tzinfo=UTC), "inflation", "failed",
            status="failed",
        )
        archived_version_run = store(
            "archived-version", "Archived CPI release", datetime(2026, 9, 4, 14, tzinfo=UTC), "inflation", "archived-version",
        )
        malformed_run = store(
            "malformed", "Malformed CPI release", datetime(2026, 9, 7, 14, tzinfo=UTC), "inflation", "malformed",
        )
        with runtime.transaction() as connection:
            for run_id, finished_at, available_at in (
                (old_run, cutoff - timedelta(hours=4), cutoff - timedelta(hours=2)),
                (latest_run, cutoff - timedelta(hours=2), cutoff - timedelta(hours=2)),
                (future_run, cutoff - timedelta(hours=1), cutoff + timedelta(hours=1)),
                (intraday_run, cutoff - timedelta(hours=2), cutoff - timedelta(hours=2)),
                (weeks_run, cutoff - timedelta(hours=2), cutoff - timedelta(hours=2)),
                (long_run, cutoff - timedelta(hours=2), cutoff - timedelta(hours=2)),
                (beyond_run, cutoff - timedelta(hours=2), cutoff - timedelta(hours=2)),
                (failed_run, cutoff - timedelta(hours=2), cutoff - timedelta(hours=2)),
                (archived_version_run, cutoff - timedelta(hours=2), cutoff - timedelta(hours=2)),
                (malformed_run, cutoff - timedelta(hours=2), cutoff - timedelta(hours=2)),
            ):
                connection.execute(
                    "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                    [finished_at - timedelta(minutes=1), finished_at, run_id],
                )
                connection.execute(
                    "UPDATE raw.market_event_version SET available_at = %s WHERE ingest_run_id = %s",
                    [available_at, run_id],
                )
            connection.execute(
                "UPDATE raw.market_event_version SET verification_status = 'archived' WHERE ingest_run_id = %s",
                [archived_version_run],
            )
            connection.execute(
                "UPDATE raw.market_event_version SET title = ' ' WHERE ingest_run_id = %s",
                [malformed_run],
            )
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = NULL WHERE id = %s",
                [cutoff - timedelta(hours=2), unfinished_run],
            )
            connection.execute(
                "UPDATE raw.market_event_version SET available_at = %s WHERE ingest_run_id = %s",
                [cutoff - timedelta(hours=2), unfinished_run],
            )
            _backdate_source(connection, source_id, cutoff)

        result = refresh_market_publication(runtime, now=cutoff)
        repository = AnalysisRepository(runtime)
        snapshot = MarketStateSnapshot.model_validate(
            repository.publication_rows("market", "market_state_snapshot")[0]
        )
        states = {
            horizon: next(dimension for dimension in dimensions if dimension.dimension == "event risk")
            for horizon, dimensions in snapshot.horizons.items()
        }
        assert result["available_coverage_rows"] == 4
        assert all(state.evidence_status == "available" for state in states.values())
        assert {state.state for state in states.values()} == {"scheduled"}
        schedules_by_horizon = {
            horizon: state.model_dump(mode="json")["scheduled_events"]
            for horizon, state in states.items()
        }
        assert {
            horizon: [event["title"] for event in events]
            for horizon, events in schedules_by_horizon.items()
        } == {
            "intraday": ["Intraday FOMC briefing"],
            "1-5 trading days": ["Latest CPI release"],
            "2-8 weeks": ["Weeks PPI release"],
            "3-12 months": ["Long FOMC meeting"],
        }
        assert all(
            title not in [event["title"] for events in schedules_by_horizon.values() for event in events]
            for title in (
                "Old CPI release", "Future CPI release", "Unfinished CPI release", "Failed CPI release",
                "Archived CPI release", "Malformed CPI release", "Beyond long horizon",
            )
        )
        schedules = [event for events in schedules_by_horizon.values() for event in events]
        assert len({event["title"] for event in schedules}) == len(schedules)
        assert all(state.probability is None for state in states.values())
        assert all("score" not in state.model_dump(mode="json") for state in states.values())
        assert all("probability" not in event for event in schedules)

        event_lineage = [
            item.model_dump(mode="json")
            for item in snapshot.input_lineage
            if item.field == "market_event_schedule"
        ]
        assert len(event_lineage) == 4
        assert all(item["fact_table"] == "raw.market_event_version" for item in event_lineage)
        assert all(item["source_id"] == source_id for item in event_lineage)
        assert all(item["source_version"] and item["ingest_run_id"] for item in event_lineage)
        assert all(item["market_event_id"] and item["fact_id"] for item in event_lineage)
        assert all(
            datetime.fromisoformat(item["cutoff"].replace("Z", "+00:00")) == cutoff
            for item in event_lineage
        )
        assert all(
            datetime.fromisoformat(item["available_at"].replace("Z", "+00:00")) <= cutoff
            for item in event_lineage
        )
        with runtime.read() as connection:
            latest_version = connection.execute(
                "SELECT id FROM raw.market_event_version WHERE ingest_run_id = %s", [latest_run]
            ).fetchone()["id"]
        assert latest_version in {item["fact_id"] for item in event_lineage}

        coverage = [row for row in snapshot.coverage_matrix.rows if row.dimension == "event risk"]
        assert len(coverage) == 4
        assert all(row.current_status == "available" and row.decision_impact == "context" for row in coverage)
        assert all(not row.blockers for row in coverage)
        assert all(row.point_in_time_safe and row.input_lineage for row in coverage)
        assert sorted(json.dumps(item.model_dump(mode="json"), sort_keys=True) for item in snapshot.input_lineage) == sorted(
            json.dumps(item.model_dump(mode="json"), sort_keys=True)
            for row in coverage
            for item in row.input_lineage
        )
        assert len(snapshot.coverage_matrix.rows) == 48
        assert len({f"{row.horizon}:{row.dimension}:{row.asset_class}" for row in snapshot.coverage_matrix.rows}) == 48
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE raw.market_event_version SET available_at = %s WHERE ingest_run_id = %s",
                [cutoff - timedelta(hours=1), latest_run],
            )
        changed_result = refresh_market_publication(runtime, now=cutoff)
        assert changed_result["snapshot_id"] != result["snapshot_id"]
    finally:
        runtime.close()


def test_market_event_risk_uses_exact_calendar_boundaries(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    facts = SourceFactRepository(runtime)
    source_id = "official-event-calendar"
    cutoff = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
    market_dates = []
    cursor = cutoff.date() + timedelta(days=1)
    while len(market_dates) < 253:
        if is_us_market_day(cursor):
            market_dates.append(cursor)
        cursor += timedelta(days=1)
    assert [day.isoformat() for day in market_dates[:3]] == ["2026-11-25", "2026-11-27", "2026-11-30"]
    ingestion.register_source(
        source_id, name="Official events", family="events", kind="calendar", origin="test",
        operational_state="active", health_owner="update_event_calendar", freshness_seconds=86400,
    )
    run_id = ingestion.start_run(source_id, "macro_events", source_run_key="boundaries")
    rows = [
        {
            "source_key": f"boundary-{days}", "event_scope": "macro", "event_kind": "calendar",
            "title": f"{days}-trading-day event",
            "starts_at": datetime.combine(market_dates[days - 1], time(14), tzinfo=UTC),
            "verification_status": "scheduled",
        }
        for days in (5, 6, 40, 41, 252, 253)
    ]
    assert facts.store_market_events(run_id, source_id, rows) == len(rows)
    ingestion.finish_run(run_id, "succeeded")
    try:
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [cutoff - timedelta(minutes=10), cutoff - timedelta(minutes=10), run_id],
            )
            connection.execute(
                "UPDATE raw.market_event_version SET available_at = %s WHERE ingest_run_id = %s",
                [cutoff - timedelta(minutes=10), run_id],
            )
        result = refresh_market_publication(runtime, now=cutoff)
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
        states = {
            horizon: next(dimension for dimension in dimensions if dimension.dimension == "event risk")
            for horizon, dimensions in snapshot.horizons.items()
        }
        schedules = {
            horizon: [event["title"] for event in state.model_dump(mode="json").get("scheduled_events", [])]
            for horizon, state in states.items()
        }
        assert result["available_coverage_rows"] == 3
        assert states["intraday"].evidence_status == "unavailable"
        assert states["intraday"].blockers == ("event_risk_inputs_unavailable",)
        assert schedules["1-5 trading days"] == ["5-trading-day event"]
        assert schedules["2-8 weeks"] == ["6-trading-day event", "40-trading-day event"]
        assert schedules["3-12 months"] == ["41-trading-day event", "252-trading-day event"]
        assert all(
            "253-trading-day event" not in events
            for events in schedules.values()
        )
    finally:
        runtime.close()


def test_market_event_risk_stays_unavailable_for_out_of_range_only_event(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    facts = SourceFactRepository(runtime)
    source_id = "official-event-calendar"
    cutoff = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)
    ingestion.register_source(
        source_id, name="Official events", family="events", kind="calendar", origin="test",
        operational_state="active", health_owner="update_event_calendar", freshness_seconds=86400,
    )
    run_id = ingestion.start_run(source_id, "macro_events", source_run_key="out-of-range")
    assert facts.store_market_events(run_id, source_id, [{
        "source_key": "out-of-range-event", "event_scope": "macro", "event_kind": "inflation",
        "title": "Beyond 252 trading days", "starts_at": datetime(2027, 10, 1, 14, tzinfo=UTC),
        "verification_status": "scheduled",
    }]) == 1
    ingestion.finish_run(run_id, "succeeded")
    try:
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [cutoff - timedelta(minutes=10), cutoff - timedelta(minutes=10), run_id],
            )
            connection.execute(
                "UPDATE raw.market_event_version SET available_at = %s WHERE ingest_run_id = %s",
                [cutoff - timedelta(minutes=10), run_id],
            )
        result = refresh_market_publication(runtime, now=cutoff)
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
        states = [
            dimension
            for dimensions in snapshot.horizons.values()
            for dimension in dimensions
            if dimension.dimension == "event risk"
        ]
        coverage = [row for row in snapshot.coverage_matrix.rows if row.dimension == "event risk"]
        assert result["available_coverage_rows"] == 0
        assert all(state.evidence_status == "unavailable" for state in states)
        assert all(state.blockers == ("event_risk_inputs_unavailable",) and not state.lineage for state in states)
        assert all(row.current_status == "unavailable" for row in coverage)
        assert all(
            tuple(row.blockers) == ("event_risk_inputs_unavailable",) and not row.input_lineage
            for row in coverage
        )
    finally:
        runtime.close()


def test_market_event_risk_stays_unavailable_for_disabled_fresh_source(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    facts = SourceFactRepository(runtime)
    source_id = "official-event-calendar"
    cutoff = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)
    ingestion.register_source(
        source_id, name="Official events", family="events", kind="calendar", origin="test",
        operational_state="active", health_owner="update_event_calendar", freshness_seconds=3600,
    )
    run_id = ingestion.start_run(source_id, "macro_events", source_run_key="disabled-fresh")
    assert facts.store_market_events(run_id, source_id, [{
        "source_key": "disabled-fresh-event", "event_scope": "macro", "event_kind": "inflation",
        "title": "Fresh CPI release", "starts_at": datetime(2026, 9, 1, 14, tzinfo=UTC),
        "verification_status": "confirmed",
    }]) == 1
    ingestion.finish_run(run_id, "succeeded")
    try:
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [cutoff - timedelta(minutes=10), cutoff - timedelta(minutes=10), run_id],
            )
            connection.execute(
                "UPDATE raw.market_event_version SET available_at = %s WHERE ingest_run_id = %s",
                [cutoff - timedelta(minutes=10), run_id],
            )
        ingestion.set_source_enabled(source_id, False)
        refresh_market_publication(runtime, now=cutoff)
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
        states = [
            dimension
            for dimensions in snapshot.horizons.values()
            for dimension in dimensions
            if dimension.dimension == "event risk"
        ]
        coverage = [row for row in snapshot.coverage_matrix.rows if row.dimension == "event risk"]
        assert all(state.evidence_status == "unavailable" for state in states)
        assert all(state.blockers == ("event_risk_inputs_unavailable",) for state in states)
        assert all(row.current_status == "unavailable" for row in coverage)
        assert all(tuple(row.blockers) == ("event_risk_inputs_unavailable",) for row in coverage)
    finally:
        runtime.close()


def test_market_event_risk_stays_unavailable_for_stale_disabled_or_empty_sources(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    facts = SourceFactRepository(runtime)
    source_id = "official-event-calendar"
    cutoff = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)
    ingestion.register_source(
        source_id, name="Official events", family="events", kind="calendar", origin="test",
        operational_state="active", health_owner="update_event_calendar", freshness_seconds=3600,
    )
    run_id = ingestion.start_run(source_id, "macro_events", source_run_key="stale")
    assert facts.store_market_events(run_id, source_id, [{
        "source_key": "stale-event", "event_scope": "macro", "event_kind": "inflation",
        "title": "Stale CPI release", "starts_at": datetime(2026, 9, 1, 14, tzinfo=UTC),
        "verification_status": "confirmed",
    }]) == 1
    ingestion.finish_run(run_id, "succeeded")

    def event_states() -> dict[str, object]:
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
        return {
            horizon: next(dimension for dimension in dimensions if dimension.dimension == "event risk")
            for horizon, dimensions in snapshot.horizons.items()
        }

    def assert_unavailable() -> None:
        states = event_states()
        assert all(state.evidence_status == "unavailable" for state in states.values())
        assert all(state.blockers == ("event_risk_inputs_unavailable",) for state in states.values())
        assert all(not state.lineage for state in states.values())

    try:
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [cutoff - timedelta(minutes=10), cutoff - timedelta(minutes=10), run_id],
            )
            connection.execute(
                "UPDATE raw.market_event_version SET available_at = %s WHERE ingest_run_id = %s",
                [cutoff - timedelta(minutes=10), run_id],
            )
            connection.execute(
                "UPDATE ingest.source SET operational_state = 'archived' WHERE id = %s", [source_id]
            )
        refresh_market_publication(runtime, now=cutoff)
        assert_unavailable()

        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.source SET operational_state = 'active' WHERE id = %s", [source_id]
            )
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [cutoff - timedelta(days=2), cutoff - timedelta(days=2), run_id],
            )
            connection.execute(
                "UPDATE raw.market_event_version SET available_at = %s WHERE ingest_run_id = %s",
                [cutoff - timedelta(days=2), run_id],
            )
        refresh_market_publication(runtime, now=cutoff)
        assert_unavailable()

        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET finished_at = %s WHERE id = %s",
                [cutoff - timedelta(minutes=10), run_id],
            )
            connection.execute(
                "UPDATE raw.market_event_version SET available_at = %s, starts_at = %s WHERE ingest_run_id = %s",
                [cutoff - timedelta(minutes=10), datetime(2026, 9, 1, 14, tzinfo=UTC), run_id],
            )
        ingestion.set_source_enabled(source_id, False)
        refresh_market_publication(runtime, now=cutoff)
        assert_unavailable()

        ingestion.set_source_enabled(source_id, True)
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET finished_at = %s WHERE id = %s",
                [cutoff - timedelta(minutes=10), run_id],
            )
            connection.execute(
                "UPDATE raw.market_event_version SET available_at = %s, starts_at = %s WHERE ingest_run_id = %s",
                [cutoff - timedelta(minutes=10), cutoff - timedelta(minutes=1), run_id],
            )
        refresh_market_publication(runtime, now=cutoff)
        assert_unavailable()
    finally:
        runtime.close()


def _corporate_fact(
    symbol: str,
    period_start: str,
    period_end: str,
    accepted_at: datetime,
    accession: str,
    revenue: float,
    operating_income: float,
    *,
    asset_class: str = "equity",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "asset_class": asset_class,
        "period_start": period_start,
        "period_end": period_end,
        "filed_at": accepted_at,
        "observed_at": accepted_at,
        "values": {
            "accession_number": accession,
            "accepted_at": accepted_at.isoformat(),
            "form": "10-K",
            "fiscal_period": "FY",
            "metrics": {"revenue": revenue, "operating_income": operating_income},
            "tags": {
                "revenue": {"unit": "USD"},
                "operating_income": {"unit": "USD"},
            },
        },
    }


def _complete_corporate_rows() -> list[dict[str, object]]:
    return [
        _corporate_fact("ACME", "2025-01-01", "2025-12-31", datetime(2026, 2, 1, tzinfo=UTC), "ACME-2025", 120, 18),
        _corporate_fact("ACME", "2024-01-01", "2024-12-31", datetime(2025, 2, 1, tzinfo=UTC), "ACME-2024", 100, 10),
        _corporate_fact("BETA", "2025-01-01", "2025-12-31", datetime(2026, 2, 2, tzinfo=UTC), "BETA-2025", 210, 21),
        _corporate_fact("BETA", "2024-01-01", "2024-12-31", datetime(2025, 2, 2, tzinfo=UTC), "BETA-2024", 200, 18),
        _corporate_fact("QQQ", "2025-01-01", "2025-12-31", datetime(2026, 2, 3, tzinfo=UTC), "QQQ-2025", 230, 34.5),
        _corporate_fact("QQQ", "2024-01-01", "2024-12-31", datetime(2025, 2, 3, tzinfo=UTC), "QQQ-2024", 200, 28),
        _corporate_fact("NVDA", "2025-01-01", "2025-12-31", datetime(2026, 2, 4, tzinfo=UTC), "NVDA-2025", 230, 34.5),
        _corporate_fact("NVDA", "2024-01-01", "2024-12-31", datetime(2025, 2, 4, tzinfo=UTC), "NVDA-2024", 200, 28),
        _corporate_fact("SPY", "2025-01-01", "2025-12-31", datetime(2026, 2, 3, tzinfo=UTC), "SPY-2025", 1000, 100, asset_class="etf"),
    ]


def _backdate_corporate_authority(connection, cutoff: datetime) -> None:
    visible_at = cutoff - timedelta(days=1)
    connection.execute(
        "UPDATE catalog.instrument SET created_at = %s "
        "WHERE symbol = ANY(%s)",
        [visible_at, ["ACME", "BETA", "NVDA", "QQQ", "SPY"]],
    )
    connection.execute(
        """
        INSERT INTO app.watchlist_item
            (instrument_id, watch_state, created_at, updated_at)
        SELECT id, 'watched', %s, %s
        FROM catalog.instrument
        WHERE symbol = ANY(%s)
        ON CONFLICT (instrument_id) DO UPDATE
        SET watch_state = 'watched', created_at = EXCLUDED.created_at,
            updated_at = EXCLUDED.updated_at
        """,
        [visible_at, visible_at, ["ACME", "BETA", "NVDA", "QQQ", "SPY"]],
    )
    _backdate_source(connection, "sec_companyfacts", cutoff)


def test_market_corporate_cycle_uses_cutoff_visible_annual_sec_facts(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source(
            "sec_companyfacts", name="SEC company facts", family="fundamentals", kind="sec_companyfacts"
        )
        run_id = ingestion.start_run("sec_companyfacts", "company_financials")
        cutoff = datetime(2026, 8, 28, 15, tzinfo=UTC)
        rows = _complete_corporate_rows()
        assert ingestion.store_fundamental_observations(
            run_id, "sec_companyfacts", "sec_companyfacts", rows
        ) == len(rows)
        ingestion.finish_run(run_id, "succeeded")
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [cutoff - timedelta(minutes=30), cutoff - timedelta(minutes=30), run_id],
            )
            _backdate_corporate_authority(connection, cutoff)
        first = refresh_market_publication(runtime, now=cutoff)
        repository = AnalysisRepository(runtime)
        snapshot = MarketStateSnapshot.model_validate(
            repository.publication_rows("market", "market_state_snapshot")[0]
        )
        states = {
            horizon: next(item for item in dimensions if item.dimension == "corporate cycle")
            for horizon, dimensions in snapshot.horizons.items()
        }
        corporate = states["3-12 months"]
        assert corporate.evidence_status == "available"
        assert corporate.state == "reported annual issuer actuals"
        assert corporate.model_dump(mode="json")["median_revenue_growth"] == pytest.approx(0.15)
        assert corporate.model_dump(mode="json")["median_operating_margin_change_bps"] == pytest.approx(100)
        assert corporate.model_dump(mode="json")["eligible_member_count"] == 4
        assert corporate.model_dump(mode="json")["available_member_count"] == 4
        assert corporate.model_dump(mode="json")["selected_periods"]
        assert len(corporate.lineage) == 8
        assert all(item.source_id == "sec_companyfacts" for item in corporate.lineage)
        assert all(item.fact_table == "raw.fundamental_observation" for item in corporate.lineage)
        assert all(item.cutoff == cutoff for item in corporate.lineage)
        assert all(item.available_at <= cutoff for item in corporate.lineage)
        for horizon in ("intraday", "1-5 trading days", "2-8 weeks"):
            assert states[horizon].evidence_status == "unavailable"
            assert not states[horizon].lineage
            assert states[horizon].blockers == market_analysis._CORPORATE_HORIZON_BLOCKERS[horizon][:1]
        coverage = [row for row in snapshot.coverage_matrix.rows if row.dimension == "corporate cycle"]
        assert len(coverage) == 4
        assert sum(row.current_status == "available" for row in coverage) == 1
        assert len(snapshot.input_lineage) == 8
        assert first["snapshot_id"] == snapshot.snapshot_id

        amendment_run = ingestion.start_run("sec_companyfacts", "company_financials")
        assert ingestion.store_fundamental_observations(
            amendment_run,
            "sec_companyfacts",
            "sec_companyfacts",
            [_corporate_fact("ACME", "2025-01-01", "2025-12-31", datetime(2026, 8, 28, 16, tzinfo=UTC), "ACME-2025-A", 150, 30)],
        ) == 1
        ingestion.finish_run(amendment_run, "succeeded")
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [cutoff - timedelta(minutes=20), cutoff - timedelta(minutes=20), amendment_run],
            )
            _backdate_corporate_authority(connection, cutoff)
        before_amendment = refresh_market_publication(runtime, now=cutoff)
        assert before_amendment["snapshot_id"] == first["snapshot_id"]

        after_amendment = refresh_market_publication(
            runtime, now=datetime(2026, 8, 28, 17, tzinfo=UTC)
        )
        changed = MarketStateSnapshot.model_validate(
            repository.publication_rows("market", "market_state_snapshot")[0]
        )
        changed_corporate = next(
            item for item in changed.horizons["3-12 months"] if item.dimension == "corporate cycle"
        )
        assert after_amendment["snapshot_id"] != first["snapshot_id"]
        assert changed_corporate.model_dump(mode="json")["median_revenue_growth"] == pytest.approx(0.15)
        assert any("ACME-2025-A" == item.accession_number for item in changed_corporate.lineage)

        source_cutoff = datetime(2026, 8, 28, 17, tzinfo=UTC)
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.source SET created_at = %s WHERE id = 'sec_companyfacts'",
                [source_cutoff + timedelta(minutes=1)],
            )
        refresh_market_publication(runtime, now=source_cutoff)
        future_source = MarketStateSnapshot.model_validate(
            repository.publication_rows("market", "market_state_snapshot")[0]
        )
        future_corporate = next(
            item for item in future_source.horizons["3-12 months"]
            if item.dimension == "corporate cycle"
        )
        assert future_corporate.evidence_status == "unavailable"
        assert future_corporate.blockers == ("corporate_cycle_source_missing",)
    finally:
        runtime.close()


def test_market_corporate_cycle_rejects_stale_facts_after_fresh_incomplete_run(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source(
            "sec_companyfacts", name="SEC company facts", family="fundamentals", kind="sec_companyfacts"
        )
        cutoff = datetime(2026, 8, 28, 15, tzinfo=UTC)
        old_run = ingestion.start_run("sec_companyfacts", "company_financials")
        old_rows = [row for row in _complete_corporate_rows() if row["symbol"] != "SPY"]
        for row in old_rows:
            if row["symbol"] == "NVDA":
                row["values"]["form"] = "10-Q"
        assert ingestion.store_fundamental_observations(
            old_run, "sec_companyfacts", "sec_companyfacts", old_rows
        ) == len(old_rows)
        ingestion.finish_run(old_run, "succeeded")
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [cutoff - timedelta(days=2), cutoff - timedelta(days=2), old_run],
            )
            _backdate_corporate_authority(connection, cutoff)
        fresh_incomplete_run = ingestion.start_run("sec_companyfacts", "company_financials")
        ingestion.finish_run(fresh_incomplete_run, "succeeded")
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [cutoff - timedelta(minutes=30), cutoff - timedelta(minutes=30), fresh_incomplete_run],
            )

        refresh_market_publication(runtime, now=cutoff)
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
        state = next(item for item in snapshot.horizons["3-12 months"] if item.dimension == "corporate cycle")
        coverage = next(row for row in snapshot.coverage_matrix.rows if row.dimension == "corporate cycle" and row.horizon == "3-12 months")
        for item in (state, coverage):
            assert item.benchmark_key == "market-corporate-equity"
            assert item.eligible_member_count == 4
            assert item.available_member_count == 0
            assert item.missing_member_count == 1
            assert item.stale_member_count == 3
            assert item.duplicate_member_count == 0
            assert item.invalid_member_count == 0
        assert state.evidence_status == "unavailable"
        assert not state.lineage
        assert coverage.current_status == "unavailable"
        assert not coverage.input_lineage
    finally:
        runtime.close()


@pytest.mark.parametrize("failure", ("duplicate", "unit", "period"))
def test_market_corporate_cycle_rejects_invalid_annual_evidence(
    migrated_postgres_dsn: str,
    failure: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source(
            "sec_companyfacts", name="SEC company facts", family="fundamentals", kind="sec_companyfacts"
        )
        cutoff = datetime(2026, 8, 28, 15, tzinfo=UTC)
        rows = [row for row in _complete_corporate_rows() if row["symbol"] != "SPY"]
        if failure == "duplicate":
            rows.append(_corporate_fact("ACME", "2025-01-01", "2025-12-31", datetime(2026, 2, 5, tzinfo=UTC), "ACME-2025", 120, 18))
        elif failure == "unit":
            rows[0]["values"]["tags"]["revenue"]["unit"] = "EUR"
        else:
            rows[1]["period_start"] = "2023-01-01"
            rows[1]["period_end"] = "2023-12-31"
        run_id = ingestion.start_run("sec_companyfacts", "company_financials")
        assert ingestion.store_fundamental_observations(
            run_id, "sec_companyfacts", "sec_companyfacts", rows
        ) == len(rows)
        ingestion.finish_run(run_id, "succeeded")
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [cutoff - timedelta(minutes=30), cutoff - timedelta(minutes=30), run_id],
            )
            _backdate_corporate_authority(connection, cutoff)

        refresh_market_publication(runtime, now=cutoff)
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
        state = next(item for item in snapshot.horizons["3-12 months"] if item.dimension == "corporate cycle")
        coverage = next(row for row in snapshot.coverage_matrix.rows if row.dimension == "corporate cycle" and row.horizon == "3-12 months")
        assert state.evidence_status == "unavailable"
        assert state.eligible_member_count == 4
        assert state.available_member_count == 3
        assert state.missing_member_count == 0
        assert state.stale_member_count == 0
        assert state.duplicate_member_count == (1 if failure == "duplicate" else 0)
        assert state.invalid_member_count == (1 if failure != "duplicate" else 0)
        assert coverage.benchmark_key == "market-corporate-equity"
        assert coverage.eligible_member_count == 4
        assert coverage.available_member_count == 3
        assert coverage.duplicate_member_count == (1 if failure == "duplicate" else 0)
        assert coverage.invalid_member_count == (1 if failure != "duplicate" else 0)
        assert not state.lineage
        assert not coverage.input_lineage
    finally:
        runtime.close()


def test_market_corporate_cycle_snapshot_id_tracks_selected_metric_values(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source(
            "sec_companyfacts", name="SEC company facts", family="fundamentals", kind="sec_companyfacts"
        )
        cutoff = datetime(2026, 8, 28, 15, tzinfo=UTC)
        run_id = ingestion.start_run("sec_companyfacts", "company_financials")
        rows = [row for row in _complete_corporate_rows() if row["symbol"] != "SPY"]
        assert ingestion.store_fundamental_observations(
            run_id, "sec_companyfacts", "sec_companyfacts", rows
        ) == len(rows)
        ingestion.finish_run(run_id, "succeeded")
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [cutoff - timedelta(minutes=30), cutoff - timedelta(minutes=30), run_id],
            )
            _backdate_corporate_authority(connection, cutoff)
        repository = AnalysisRepository(runtime)
        first = refresh_market_publication(runtime, now=cutoff)
        with runtime.transaction() as connection:
            connection.execute(
                """
                UPDATE raw.fundamental_observation
                SET values = jsonb_set(jsonb_set(values, '{metrics,revenue}', '130'::jsonb),
                                       '{metrics,operating_income}', '19.5'::jsonb)
                WHERE instrument_id = (SELECT id FROM catalog.instrument WHERE symbol = 'ACME')
                  AND source_id = 'sec_companyfacts'
                  AND metric_set = 'sec_companyfacts'
                  AND period_end = DATE '2025-12-31'
                """
            )
        second = refresh_market_publication(runtime, now=cutoff)
        changed = MarketStateSnapshot.model_validate(
            repository.publication_rows("market", "market_state_snapshot")[0]
        )
        state = next(item for item in changed.horizons["3-12 months"] if item.dimension == "corporate cycle")
        assert second["snapshot_id"] != first["snapshot_id"]
        assert state.median_revenue_growth == pytest.approx(0.15)
        assert state.median_operating_margin_change_bps == pytest.approx(100)
        acme = next(item for item in state.selected_periods if item["symbol"] == "ACME")
        assert acme["latest"]["revenue"] == pytest.approx(130)
        assert acme["latest"]["operating_income"] == pytest.approx(19.5)
    finally:
        runtime.close()


def test_market_corporate_cycle_rejects_malformed_metric_tag_metadata(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source(
            "sec_companyfacts", name="SEC company facts", family="fundamentals", kind="sec_companyfacts"
        )
        cutoff = datetime(2026, 8, 28, 15, tzinfo=UTC)
        rows = [row for row in _complete_corporate_rows() if row["symbol"] != "SPY"]
        rows[0]["values"]["tags"]["revenue"] = "malformed"
        run_id = ingestion.start_run("sec_companyfacts", "company_financials")
        assert ingestion.store_fundamental_observations(
            run_id, "sec_companyfacts", "sec_companyfacts", rows
        ) == len(rows)
        ingestion.finish_run(run_id, "succeeded")
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [cutoff - timedelta(minutes=30), cutoff - timedelta(minutes=30), run_id],
            )
            _backdate_corporate_authority(connection, cutoff)

        refresh_market_publication(runtime, now=cutoff)
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
        state = next(item for item in snapshot.horizons["3-12 months"] if item.dimension == "corporate cycle")
        coverage = next(row for row in snapshot.coverage_matrix.rows if row.dimension == "corporate cycle" and row.horizon == "3-12 months")
        assert state.evidence_status == "unavailable"
        assert state.available_member_count == 3
        assert state.invalid_member_count == 1
        assert "corporate_cycle_annual_fact_invalid" in state.blockers
        assert coverage.invalid_member_count == 1
        assert not state.lineage
        assert not coverage.input_lineage
    finally:
        runtime.close()


def test_historical_market_publication_omits_future_catalog_and_membership_rows(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        symbols = (
            "PITPOS", "PITWATCH", "PITFUTI", "PITNOISY", "PITFUTP", "PITPROJ", "PITCLOSED",
            "PITFUTW", "PITREVW", "PITCONFIG", "PITCATALOG",
        )
        cutoff = datetime.now(UTC) + timedelta(minutes=1)
        before = cutoff - timedelta(days=1)
        after = cutoff + timedelta(days=1)
        with runtime.transaction() as connection:
            connection.execute(
                """
                INSERT INTO catalog.instrument
                    (symbol, name, asset_class, created_at, updated_at)
                SELECT symbol, symbol, 'equity',
                       CASE WHEN symbol = 'PITFUTI' THEN %s ELSE %s END,
                       CASE WHEN symbol IN ('PITFUTI', 'PITNOISY') THEN %s ELSE %s END
                FROM unnest(%s::text[]) AS symbol
                """,
                [after, before, after, before, list(symbols)],
            )
            ids = {
                row["symbol"]: row["id"]
                for row in connection.execute(
                    "SELECT id, symbol FROM catalog.instrument WHERE symbol = ANY(%s)",
                    [list(symbols)],
                ).fetchall()
            }
            connection.execute(
                """
                INSERT INTO app.portfolio_transaction
                    (instrument_id, transaction_type, quantity, price, amount,
                     executed_at, created_at, idempotency_key)
                VALUES
                    (%s, 'opening_balance', 1, 10, 10, %s, %s, 'market-pit-position'),
                    (%s, 'buy', 1, 11, 11, %s, %s, 'market-pit-later-buy'),
                    (%s, 'opening_balance', 1, 10, 10, %s, %s, 'market-pit-noisy'),
                    (%s, 'opening_balance', 1, 10, 10, %s, %s, 'market-pit-future'),
                    (%s, 'opening_balance', 1, 10, 10, %s, %s, 'market-pit-closed-open'),
                    (%s, 'sell', 1, 10, 10, %s, %s, 'market-pit-closed-sell')
                """,
                [
                    ids["PITPOS"], before, before,
                    ids["PITPOS"], after, after,
                    ids["PITNOISY"], before, before,
                    ids["PITFUTP"], after, after,
                    ids["PITCLOSED"], before - timedelta(hours=1), before - timedelta(hours=1),
                    ids["PITCLOSED"], before, before,
                ],
            )
            connection.execute(
                """
                INSERT INTO app.portfolio_position (instrument_id, quantity, updated_at)
                VALUES (%s, 2, %s), (%s, 1, %s), (%s, 1, %s)
                """,
                [
                    ids["PITPOS"], after,
                    ids["PITFUTP"], after,
                    ids["PITPROJ"], before,
                ],
            )
            connection.execute(
                "INSERT INTO app.watchlist_item "
                "(instrument_id, watch_state, created_at, updated_at) VALUES "
                "(%s, 'watched', %s, %s), (%s, 'watched', %s, %s), "
                "(%s, 'watched', %s, %s)",
                [
                    ids["PITWATCH"], before, before,
                    ids["PITFUTW"], after, after,
                    ids["PITREVW"], before, after,
                ],
            )

        refresh_market_publication(
            runtime,
            now=cutoff,
            configured_watchlist=[
                {"symbol": " pitconfig "},
                {"symbol": "PITCATALOG", "watch_state": "excluded"},
            ],
        )
        repository = AnalysisRepository(runtime)
        snapshot = MarketStateSnapshot.model_validate(
            repository.publication_rows("market", "market_state_snapshot")[0]
        )
        state = next(
            item for item in snapshot.horizons["3-12 months"]
            if item.dimension == "corporate cycle"
        )
        assert tuple(state.eligible_members) == (
            "PITNOISY", "PITPOS", "PITWATCH",
        )
    finally:
        runtime.close()
