from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta

import pytest

from app.data_access.loaders import load_panel_scope_data
from investment_panel.core.decision import MARKET_DIMENSIONS, MARKET_HORIZONS, MarketStateSnapshot, is_us_market_day
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.confirmed_daily_prices import completed_trading_dates
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.market_analysis import refresh_market_publication
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.source_facts import SourceFactRepository
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
