"""0034 must be observationally equivalent to 0030, with bounded quote work."""

from datetime import UTC, datetime, timedelta
from importlib import import_module

import psycopg
from psycopg.rows import dict_row

from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime


def _reference(connection):
    sql = import_module(
        "migrations.versions.20260921_0030_current_price_information_time"
    )._function(information_time_first=True)
    connection.execute(sql.replace(
        "FUNCTION raw.current_price_for_instruments(",
        "FUNCTION raw.test_reference_current_price(", 1,
    ))


def _compare(connection, instrument_id, cutoff):
    args = [cutoff, [instrument_id]]
    expected = connection.execute(
        "SELECT * FROM raw.test_reference_current_price(%s, %s::bigint[])", args,
    ).fetchall()
    actual = connection.execute(
        "SELECT * FROM raw.current_price_for_instruments(%s, %s::bigint[])", args,
    ).fetchall()
    assert actual == expected
    return actual


def _source(repository, source, kind="quote"):
    repository.register_source(
        source, name=source, family="market", kind=kind,
        operational_state="active", health_owner="test", freshness_seconds=3600,
    )


def test_confirmed_tips_preserve_versions_cutoffs_and_source_information_time(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    observed = datetime(2026, 7, 6, 15, tzinfo=UTC)
    cutoffs = []
    try:
        _source(repository, "tip-broker")
        _source(repository, "tip-second")
        _source(repository, "tip-daily", "daily_bars")
        with psycopg.connect(migrated_postgres_dsn, row_factory=dict_row) as connection:
            _reference(connection)
            connection.commit()

            def write(source, at, price, status="succeeded"):
                run = repository.start_run(source, "quotes")
                repository.store_quotes(run, source, [{"symbol": "TIP", "observed_at": at, "price": price}])
                if status != "running":
                    repository.finish_run(run, status)
                cutoffs.append(connection.execute("SELECT clock_timestamp() AS at").fetchone()["at"])
                return run

            write("tip-broker", observed, 100)
            instrument_id = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = 'TIP'"
            ).fetchone()["id"]
            # A current/history shared ID is the production correction path.
            write("tip-broker", observed, 999, "running")
            assert _compare(connection, instrument_id, cutoffs[-1])[0]["price"] == 100
            write("tip-broker", observed, 998, "failed")
            assert _compare(connection, instrument_id, cutoffs[-1])[0]["price"] == 100
            write("tip-broker", observed, 101, "partial")
            assert _compare(connection, instrument_id, cutoffs[-1])[0]["price"] == 101
            assert connection.execute(
                "SELECT EXISTS (SELECT 1 FROM raw.quote q JOIN raw.quote_history h USING (id) WHERE q.instrument_id = %s) AS shared",
                [instrument_id],
            ).fetchone()["shared"]
            # Late old replay has a newer confirmation, not newer information.
            write("tip-broker", observed - timedelta(hours=1), 42)
            assert _compare(connection, instrument_id, cutoffs[-1])[0]["price"] == 101
            write("tip-second", observed - timedelta(minutes=1), 102)
            assert _compare(connection, instrument_id, cutoffs[-1])[0]["price"] == 102
            run = repository.start_run("tip-daily", "price_bars")
            repository.store_price_bars(
                run, "tip-daily", [{"symbol": "TIP", "date": "2026-07-02", "close": 97}],
                asset_classes={"TIP": "equity"},
            )
            repository.finish_run(run, "succeeded")
            cutoffs.append(connection.execute("SELECT clock_timestamp() AS at").fetchone()["at"])
            # Re-evaluate historical cutoffs after every table has newer versions.
            for cutoff in cutoffs:
                _compare(connection, instrument_id, cutoff)
            assert _compare(connection, instrument_id, cutoffs[0])[0]["price"] == 100
    finally:
        runtime.close()


def test_confirmed_quote_tip_reduces_buffer_work_not_just_warm_wall_time(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        _source(repository, "tip-performance")
        run = repository.start_run("tip-performance", "quotes")
        start = datetime(2026, 7, 1, tzinfo=UTC)
        repository.store_quotes(run, "tip-performance", [
            {"symbol": "TIPPERF", "observed_at": start + timedelta(minutes=index), "price": 100 + index / 1000}
            for index in range(2048)
        ])
        repository.finish_run(run, "succeeded")
        with psycopg.connect(migrated_postgres_dsn, row_factory=dict_row) as connection:
            _reference(connection)
            for table in ("raw.quote", "raw.quote_history", "raw.quote_fact_availability", "ingest.run"):
                connection.execute(f"ANALYZE {table}")
            instrument_id = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = 'TIPPERF'"
            ).fetchone()["id"]
            cutoff = connection.execute("SELECT clock_timestamp() AS at").fetchone()["at"]
            _compare(connection, instrument_id, cutoff)
            plans = []
            for function in ("test_reference_current_price", "current_price_for_instruments"):
                result = connection.execute(
                    f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM raw.{function}(%s, %s::bigint[])",
                    [cutoff, [instrument_id]],
                ).fetchone()["QUERY PLAN"][0]
                plans.append(result["Plan"])
            buffers = [plan.get("Shared Hit Blocks", 0) + plan.get("Shared Read Blocks", 0) for plan in plans]
            assert buffers[0] > 0
            assert buffers[1] < buffers[0] * 0.51, {"reference_buffers": buffers[0], "tip_buffers": buffers[1], "plans": plans}
    finally:
        runtime.close()
