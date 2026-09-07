from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from investment_panel.core.decision import market_session_bounds
from investment_panel.database import price_bar_ingestion
from investment_panel.database.confirmed_daily_prices import confirmed_daily_bars
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.migrations import downgrade_database, upgrade_database
from investment_panel.database.runtime import DatabaseRuntime
from migrations.schema_contract import schema_contract


@pytest.fixture
def price_ingestion(application_postgres_dsn):
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    ingestion.register_source("session-close-test", name="Session close test", family="test", kind="daily_bars")
    try:
        yield ingestion
    finally:
        runtime.close()


def _capture(ingestion, day):
    run_id = ingestion.start_run("session-close-test", "price_bars")
    assert ingestion.store_price_bars(
        run_id, "session-close-test", [{"symbol": "QQQ", "date": day.isoformat(), "close": 500}],
        asset_classes={"QQQ": "etf"},
    ) == 1
    ingestion.finish_run(run_id, "succeeded")
    with ingestion.runtime.read() as connection:
        return connection.execute("SELECT finished_at FROM ingest.run WHERE id = %s", [run_id]).fetchone()["finished_at"]


@pytest.mark.parametrize("day,hour", [
    (date(2026, 8, 12), 20), (date(2026, 1, 2), 21),
    (date(2025, 11, 28), 18), (date(2025, 7, 3), 17), (date(2025, 12, 24), 18),
])
def test_actual_daily_bar_producer_uses_session_close_and_real_availability(price_ingestion, day, hour):
    before = datetime.now(UTC)
    finished_at = _capture(price_ingestion, day)
    with price_ingestion.runtime.read() as connection:
        bar = connection.execute("SELECT * FROM raw.price_bar").fetchone()
        quote = connection.execute("SELECT * FROM raw.quote").fetchone()
        expected = market_session_bounds(day)[1].astimezone(UTC)
        assert expected.hour == hour
        assert bar["observed_at"] == quote["observed_at"] == expected
        assert before <= bar["available_at"] <= quote["available_at"] <= finished_at
        assert confirmed_daily_bars(connection, [bar["instrument_id"]], as_of=before, trading_dates=[day], require_session_close=True) == {}
        confirmed = confirmed_daily_bars(connection, [bar["instrument_id"]], as_of=finished_at, trading_dates=[day], require_session_close=True)[bar["instrument_id"]]
        assert len(confirmed) == 1 and confirmed[0]["observed_at"] == expected
        assert confirmed[0]["available_at"] == bar["available_at"] and confirmed[0]["confirmed_at"] == finished_at
        current = connection.execute("SELECT observed_at, price FROM raw.current_price_at(%s, ARRAY[%s::bigint])", [finished_at, bar["instrument_id"]]).fetchone()
        assert current == {"observed_at": expected, "price": 500}


@pytest.mark.parametrize("day", [date(2026, 1, 2), date(2025, 11, 28)])
def test_reingestion_versions_a_legacy_daily_clock_without_backdating(price_ingestion, monkeypatch, day):
    # Exercise the old producer clock without editing any stored fact clocks.
    with monkeypatch.context() as legacy:
        legacy.setattr(price_bar_ingestion, "market_session_bounds", lambda value: (
            market_session_bounds(value)[0], datetime(value.year, value.month, value.day, 20, tzinfo=UTC),
        ))
        first_finished = _capture(price_ingestion, day)
    with price_ingestion.runtime.read() as connection:
        original = connection.execute("SELECT id, instrument_id, observed_at, available_at FROM raw.price_bar").fetchone()
        original_quote = connection.execute("SELECT id, observed_at, available_at FROM raw.quote").fetchone()
    corrected_finished = _capture(price_ingestion, day)
    with price_ingestion.runtime.read() as connection:
        corrected = connection.execute("SELECT id, observed_at, available_at FROM raw.price_bar").fetchone()
        corrected_quote = connection.execute("SELECT id, observed_at, available_at FROM raw.quote").fetchone()
        assert corrected["id"] == original["id"] and corrected_quote["id"] == original_quote["id"]
        assert corrected["observed_at"] == corrected_quote["observed_at"] == market_session_bounds(day)[1].astimezone(UTC)
        assert first_finished < corrected["available_at"] <= corrected_quote["available_at"] <= corrected_finished
        assert connection.execute("SELECT id, instrument_id, observed_at, available_at FROM raw.price_bar_history").fetchone() == original
        assert connection.execute("SELECT id, observed_at, available_at FROM raw.quote_history").fetchone() == original_quote
        old = confirmed_daily_bars(connection, [original["instrument_id"]], as_of=first_finished, trading_dates=[day], require_session_close=True)[original["instrument_id"]]
        new = confirmed_daily_bars(connection, [original["instrument_id"]], as_of=corrected_finished, trading_dates=[day], require_session_close=True)[original["instrument_id"]]
        assert len(old) == len(new) == 1 and old[0]["observed_at"] == original["observed_at"]
        assert old[0]["available_at"] == original["available_at"] and new[0]["available_at"] == corrected["available_at"]
        current = connection.execute("SELECT observed_at, available_at FROM raw.current_price_at(%s, ARRAY[%s::bigint])", [corrected_finished, original["instrument_id"]]).fetchone()
        assert current == {"observed_at": corrected["observed_at"], "available_at": corrected_quote["available_at"]}
    _capture(price_ingestion, day)
    with price_ingestion.runtime.read() as connection:
        assert connection.execute("SELECT available_at FROM raw.price_bar").fetchone()["available_at"] == corrected["available_at"]
        assert connection.execute("SELECT available_at FROM raw.quote").fetchone()["available_at"] == corrected_quote["available_at"]
        counts = connection.execute("SELECT (SELECT count(*) FROM raw.price_bar_history) AS bars, (SELECT count(*) FROM raw.quote_history) AS quotes").fetchone()
        assert counts == {"bars": 1, "quotes": 1}


def test_daily_price_clock_reader_migration_is_exactly_reversible(postgres_dsn):
    upgrade_database(postgres_dsn, "20260907_0004")
    with psycopg.connect(postgres_dsn) as connection:
        before = schema_contract(connection)
        identity = connection.execute(
            "SELECT proowner, proacl, prosecdef, provolatile, proconfig, prorettype, proargtypes "
            "FROM pg_proc WHERE oid = 'raw.current_price_for_instruments(timestamptz,bigint[])'::regprocedure"
        ).fetchone()
    upgrade_database(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        after = schema_contract(connection)
        changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
        assert len(changed) == 1 and next(iter(changed)).startswith("function:raw.current_price_for_instruments(")
        assert connection.execute(
            "SELECT proowner, proacl, prosecdef, provolatile, proconfig, prorettype, proargtypes "
            "FROM pg_proc WHERE oid = 'raw.current_price_for_instruments(timestamptz,bigint[])'::regprocedure"
        ).fetchone() == identity
    downgrade_database(postgres_dsn, "20260907_0004")
    with psycopg.connect(postgres_dsn) as connection:
        assert schema_contract(connection) == before
    upgrade_database(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        assert schema_contract(connection) == after


@pytest.mark.parametrize("day,legacy_hour", [(date(2026, 1, 2), 20), (date(2026, 8, 12), 17)])
def test_premature_legacy_winter_or_normal_day_1300_bar_stays_unconfirmed(price_ingestion, monkeypatch, day, legacy_hour):
    legacy_clock = datetime(day.year, day.month, day.day, legacy_hour, tzinfo=UTC)
    with monkeypatch.context() as legacy:
        legacy.setattr(price_bar_ingestion, "market_session_bounds", lambda value: (market_session_bounds(value)[0], legacy_clock))
        _capture(price_ingestion, day)
    # This malformed historical fixture asserts refusal, not a qualified close.
    available, confirmed = legacy_clock + timedelta(minutes=1), legacy_clock + timedelta(minutes=2)
    with price_ingestion.runtime.transaction() as connection:
        fact = connection.execute("SELECT id, instrument_id, ingest_run_id FROM raw.price_bar").fetchone()
        quote = connection.execute("SELECT id FROM raw.quote").fetchone()
        connection.execute("UPDATE ingest.run SET finished_at = %s WHERE id = %s", [confirmed, fact["ingest_run_id"]])
        connection.execute("UPDATE raw.price_bar SET available_at = %s WHERE id = %s", [available, fact["id"]])
        connection.execute("UPDATE raw.quote SET available_at = %s WHERE id = %s", [available, quote["id"]])
        connection.execute("UPDATE raw.price_bar_fact_availability SET fact_available_at = %s WHERE fact_id = %s", [available, fact["id"]])
        connection.execute("UPDATE raw.quote_fact_availability SET fact_available_at = %s WHERE fact_id = %s", [available, quote["id"]])
    with price_ingestion.runtime.read() as connection:
        for cutoff in (confirmed, market_session_bounds(day)[1].astimezone(UTC) + timedelta(minutes=1)):
            assert connection.execute("SELECT * FROM raw.current_price_at(%s, ARRAY[%s::bigint])", [cutoff, fact["instrument_id"]]).fetchone() is None
    if legacy_hour == 20:
        repaired_at = _capture(price_ingestion, day)
        with price_ingestion.runtime.read() as connection:
            repaired = connection.execute("SELECT observed_at, available_at FROM raw.current_price_at(%s, ARRAY[%s::bigint])", [repaired_at, fact["instrument_id"]]).fetchone()
            assert repaired["observed_at"] == market_session_bounds(day)[1].astimezone(UTC)
            assert repaired["available_at"] > confirmed


@pytest.mark.parametrize("symbol,asset_class,kind,expected_zone", [
    ("QQQ", "etf", "daily_quote", "America/New_York"),
    ("7203.T", "equity", "daily_bars", "Asia/Tokyo"),
    ("BTC-USD", "crypto", "daily_bars", "UTC"),
])
def test_existing_daily_quote_foreign_and_non_equity_clock_contracts_are_preserved(price_ingestion, symbol, asset_class, kind, expected_zone):
    from zoneinfo import ZoneInfo

    source = f"test-clock-compatibility-{kind}"
    day = date(2026, 1, 2)
    price_ingestion.register_source(source, name=source, family="test", kind=kind)
    run_id = price_ingestion.start_run(source, "price_bars" if kind == "daily_bars" else "quotes")
    if kind == "daily_bars":
        price_ingestion.store_price_bars(run_id, source, [{"symbol": symbol, "date": day.isoformat(), "close": 500}], asset_classes={symbol: asset_class})
    else:
        price_ingestion.store_quotes(run_id, source, [{"symbol": symbol, "price": 500, "observed_at": datetime(2026, 1, 2, 20, tzinfo=UTC)}])
    price_ingestion.finish_run(run_id, "succeeded")
    with price_ingestion.runtime.read() as connection:
        instrument_id = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = %s", [symbol]).fetchone()["id"]
        current = connection.execute("SELECT observed_at, price FROM raw.current_price_at(%s, ARRAY[%s::bigint])", [datetime.now(UTC), instrument_id]).fetchone()
        assert current == {"observed_at": datetime(2026, 1, 2, 16, tzinfo=ZoneInfo(expected_zone)), "price": 500}
