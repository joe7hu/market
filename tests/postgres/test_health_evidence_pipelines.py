"""Incident regressions through the real PostgreSQL ingestion authority."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from conftest import typed_config
from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime


@pytest.fixture
def price_store(application_postgres_dsn):
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    for source, kind in (
        ("incident-live", "crypto_quote"),
        ("incident-daily", "daily_bars"),
    ):
        repository.register_source(
            source,
            name=source,
            family="market_data",
            kind=kind,
            operational_state="active",
            health_owner="update_market_data",
            freshness_seconds=3600,
        )
    yield repository
    runtime.close()


def _daily(repository, day, *, close=100, source="incident-daily"):
    with repository.run(source, "price_bars") as run:
        repository.store_price_bars(
            run.id,
            source,
            [
                {
                    "symbol": "BTC-USD",
                    "date": day,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1,
                    "is_complete": True,
                }
            ],
            asset_classes={"BTC-USD": "crypto"},
        )
        run.finish("succeeded")


def _read(repository, cutoff=None):
    with repository.runtime.read() as connection:
        identifier = connection.execute(
            "SELECT id FROM catalog.instrument WHERE symbol = 'BTC-USD'"
        ).fetchone()["id"]
        return connection.execute(
            "SELECT * FROM raw.current_price_at(%s, ARRAY[%s::bigint])",
            [cutoff or datetime.now(UTC), identifier],
        ).fetchone()


def test_late_daily_confirmation_does_not_replace_fresh_live_quote(price_store):
    now = datetime.now(UTC)
    day = now.date() - timedelta(days=2)
    quote_at = now - timedelta(minutes=1)
    with price_store.run("incident-live", "quotes") as run:
        price_store.store_quotes(
            run.id,
            "incident-live",
            [
                {
                    "symbol": "BTC-USD",
                    "asset_class": "crypto",
                    "price": 120,
                    "observed_at": quote_at,
                }
            ],
        )
        run.finish("succeeded")
    before_daily = datetime.now(UTC)
    _daily(price_store, day)
    selected = _read(price_store)
    assert selected["price"] == 120 and selected["source_id"] == "incident-live"
    assert selected["observed_at"] == quote_at
    assert _read(price_store, before_daily)["price"] == 120


def test_crypto_daily_close_preserves_utc_bucket_and_honest_availability(price_store):
    day = datetime.now(UTC).date() - timedelta(days=2)
    _daily(price_store, day)
    expected = datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC)
    selected = _read(price_store)
    assert selected["observed_at"] == expected
    assert selected["trading_date"] == day
    assert selected["available_at"] >= expected
    assert (
        _read(price_store, expected) is None
    )  # ingesting now never backdates knowledge


def test_dependency_candles_are_exact_bounded_idempotent_and_do_not_watch_symbols(
    price_store, monkeypatch
):
    from investment_panel.workflows import daily_dependencies as workflow

    config = typed_config(raw={"watchlist": []})
    now = datetime.now(UTC)
    rows = [
        {"symbol": "BTC-USD", "asset_class": "crypto"},
        {"symbol": "ASTS", "asset_class": "equity"},
    ]
    expected = workflow.required_dates(rows, now, count=3)
    calls = []

    def candles(symbol, lookback, mode):
        calls.append((symbol, lookback))
        return pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "date": day,
                    "open": 99,
                    "high": 101,
                    "low": 98,
                    "close": 100,
                    "volume": 100,
                    "is_complete": True,
                }
                for day in expected[symbol]
            ]
        )

    monkeypatch.setattr(workflow, "fetch_prices", candles)
    result = workflow.refresh_dependencies(price_store.runtime, config, rows, count=3)
    assert result["status"] == "ok" and result["stored"] == 6
    assert {symbol for symbol, _ in calls} == {"BTC-USD", "ASTS"}
    assert max(lookback for _, lookback in calls) <= 30
    assert (
        workflow.refresh_dependencies(price_store.runtime, config, rows, count=3)[
            "stored"
        ]
        == 0
    )
    assert len(calls) == 2
    with price_store.runtime.read() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM app.watchlist_item"
            ).fetchone()["count"]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM raw.price_bar"
            ).fetchone()["count"]
            == 6
        )
        assert (
            connection.execute(
                "SELECT min(available_at) AS value FROM raw.price_bar"
            ).fetchone()["value"]
            >= now
        )


def test_dependency_provider_failure_never_substitutes_older_session_or_quote(
    price_store, monkeypatch
):
    from investment_panel.workflows import daily_dependencies as workflow

    config = typed_config(raw={"watchlist": []})
    rows = [{"symbol": "BTC-USD", "asset_class": "crypto"}]
    older = datetime.now(UTC).date() - timedelta(days=10)
    monkeypatch.setattr(
        workflow,
        "fetch_prices",
        lambda *_: pd.DataFrame(
            [{"symbol": "BTC-USD", "date": older, "close": 100, "is_complete": True}]
        ),
    )
    result = workflow.refresh_dependencies(price_store.runtime, config, rows)
    assert result["status"] == "partial" and result["stored"] == 0
    assert result["missing_symbols"] == ["BTC-USD"]
    with price_store.runtime.read() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM raw.price_bar"
            ).fetchone()["count"]
            == 0
        )


def test_dependency_future_candle_and_wrong_instrument_are_not_admitted(
    price_store, monkeypatch
):
    from investment_panel.workflows import daily_dependencies as workflow

    config = typed_config(raw={"watchlist": []})
    today = datetime.now(UTC).date()
    monkeypatch.setattr(
        workflow,
        "fetch_prices",
        lambda *_: pd.DataFrame(
            [
                {
                    "symbol": "ETH-USD",
                    "date": today - timedelta(days=1),
                    "close": 100,
                    "is_complete": True,
                },
                {"symbol": "BTC-USD", "date": today, "close": 100, "is_complete": True},
            ]
        ),
    )
    result = workflow.refresh_dependencies(
        price_store.runtime, config, [{"symbol": "BTC-USD", "asset_class": "crypto"}]
    )
    assert result["status"] == "partial" and result["stored"] == 0
    assert result["errors"]["BTC-USD"]


def test_narrow_dependency_check_cannot_reset_full_source_success_clock(
    price_store, monkeypatch
):
    from investment_panel.workflows import daily_dependencies as workflow
    from investment_panel.infrastructure.postgres.source_health import (
        SOURCE_HEALTH_QUERY,
        source_health_blockers,
    )

    workflow.register_daily_source(price_store)
    with price_store.run(workflow.SOURCE_ID, "price_bars") as run:
        run.finish("failed", failure_detail="full watched universe missing")
    day = datetime.now(UTC).date() - timedelta(days=1)
    monkeypatch.setattr(
        workflow,
        "fetch_prices",
        lambda *_: pd.DataFrame(
            [
                {
                    "symbol": "BTC-USD",
                    "date": day,
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100,
                    "volume": 1,
                    "is_complete": True,
                }
            ]
        ),
    )
    result = workflow.refresh_dependencies(
        price_store.runtime,
        typed_config(),
        [{"symbol": "BTC-USD", "asset_class": "crypto"}],
    )
    assert result["stored"] == 1
    with price_store.runtime.read() as connection:
        rows = connection.execute(SOURCE_HEALTH_QUERY).fetchall()
    source = next(row for row in rows if row["source_id"] == workflow.SOURCE_ID)
    assert source["last_success_at"] is None
    assert source["run_status"] == "failed"
    assert source_health_blockers(price_store.runtime, [workflow.SOURCE_ID])[
        workflow.SOURCE_ID
    ]


def test_portfolio_history_preserves_crypto_observation_clock(price_store):
    from investment_panel.infrastructure.postgres import portfolio_intelligence

    day = datetime.now(UTC).date() - timedelta(days=2)
    _daily(price_store, day)
    with price_store.runtime.read() as connection:
        identifier = connection.execute(
            "SELECT id FROM catalog.instrument WHERE symbol = 'BTC-USD'"
        ).fetchone()["id"]
        rows = portfolio_intelligence._confirmed_daily_price_bars(
            connection, [identifier]
        )
    assert rows[0]["observed_at"] == datetime.combine(
        day + timedelta(days=1), datetime.min.time(), UTC
    )


def test_terminal_receipt_uses_each_assets_completed_date(price_store):
    run = price_store.start_run("incident-daily", "price_bars")
    price_store.finish_run(run, "succeeded")
    expected = {"BTC-USD": "2026-09-27", "NVDA": "2026-09-25"}
    price_store.record_terminal_bar_check(
        run,
        expected_terminal_bar="2026-09-25",
        expected_terminal_bars=expected,
        missing_terminal_bars=list(expected),
        failed_symbols=list(expected),
        instrument_count=0,
    )
    with price_store.runtime.read() as connection:
        receipt = connection.execute(
            "SELECT summary, failure_detail, status FROM ingest.run WHERE id = %s",
            [run],
        ).fetchone()
    assert receipt["status"] == "partial"
    assert receipt["summary"]["expected_terminal_bars"] == expected
    assert "BTC-USD: 2026-09-27" in receipt["failure_detail"]
