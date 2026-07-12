from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime
import hashlib
from pathlib import Path

import psycopg
import pytest

from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.migrations import upgrade_database
from investment_panel.database.options import _market_session
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.jobs import update_robinhood_options


def test_option_snapshot_session_includes_listed_option_close_window() -> None:
    assert _market_session(datetime(2026, 7, 10, 20, 14, tzinfo=UTC)) == "regular"
    assert _market_session(datetime(2026, 7, 10, 20, 16, tzinfo=UTC)) == "afterhours"


@pytest.fixture
def postgres_dsn(postgresql) -> str:
    info = postgresql.info
    credentials = info.user if not info.password else f"{info.user}:{info.password}"
    return f"postgresql://{credentials}@{info.host}:{info.port}/{info.dbname}"


@pytest.fixture
def repository(postgres_dsn: str) -> IngestionRepository:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    repo = IngestionRepository(runtime)
    repo.register_source(
        "robinhood",
        name="Robinhood",
        family="broker",
        kind="option_chain",
        capabilities={"option_quotes": True},
    )
    try:
        yield repo
    finally:
        runtime.close()


def test_ingestion_run_records_one_archived_payload_manifest(repository: IngestionRepository, postgres_dsn: str) -> None:
    payload = b'{"chain": "raw-provider-payload"}'
    digest = hashlib.sha256(payload).hexdigest()
    with repository.run("robinhood", "option_quotes", source_run_key="2026-07-11-premarket") as run_id:
        first_id = repository.record_payload(
            run_id,
            "file:///archive/robinhood/2026-07-11.json.gz",
            sha256=digest,
            byte_count=len(payload),
            metadata={"compression": "gzip"},
        )
        second_id = repository.record_payload(
            run_id,
            "file:///archive/duplicate-location.json.gz",
            sha256=digest,
            byte_count=len(payload),
            metadata={"verified": True},
        )
    assert second_id == first_id

    with closing(psycopg.connect(postgres_dsn)) as connection:
        manifest = connection.execute(
            "SELECT archive_uri, byte_count, metadata FROM ingest.payload"
        ).fetchall()
        run = connection.execute(
            "SELECT status, finished_at FROM ingest.run WHERE id = %s", [run_id]
        ).fetchone()
    assert manifest == [
        ("file:///archive/robinhood/2026-07-11.json.gz", len(payload), {"compression": "gzip", "verified": True})
    ]
    assert run[0] == "succeeded"
    assert run[1] is not None


def test_failed_ingestion_run_persists_failure(repository: IngestionRepository, postgres_dsn: str) -> None:
    with pytest.raises(RuntimeError, match="provider unavailable"):
        with repository.run("robinhood", "option_quotes") as run_id:
            raise RuntimeError("provider unavailable")
    with closing(psycopg.connect(postgres_dsn)) as connection:
        row = connection.execute(
            "SELECT status, failure_detail FROM ingest.run WHERE id = %s", [run_id]
        ).fetchone()
    assert row == ("failed", "RuntimeError: provider unavailable")


def test_daily_price_bars_are_idempotent_and_materialize_latest_quote(repository: IngestionRepository) -> None:
    repository.register_source("daily-prices", name="Daily", family="market_data", kind="daily_bars")
    rows = [
        {"symbol": "QQQ", "date": "2026-07-09", "open": 600, "high": 606, "low": 598, "close": 604, "volume": 10},
        {"symbol": "QQQ", "date": "2026-07-10", "open": 604, "high": 610, "low": 603, "close": 609, "volume": 12},
    ]
    for _ in range(2):
        run_id = repository.start_run("daily-prices", "price_bars")
        assert repository.store_price_bars(run_id, "daily-prices", rows, asset_classes={"QQQ": "etf"}) == 2
        repository.finish_run(run_id, "succeeded")
    with repository.runtime.read() as connection:
        counts = connection.execute(
            "SELECT (SELECT count(*) FROM raw.price_bar) AS bars, (SELECT count(*) FROM raw.quote) AS quotes"
        ).fetchone()
        latest = connection.execute(
            """
            SELECT instrument.symbol, instrument.asset_class, quote.price, quote.observed_at::date
            FROM raw.quote quote JOIN catalog.instrument instrument ON instrument.id = quote.instrument_id
            """
        ).fetchone()
    assert (counts["bars"], counts["quotes"]) == (2, 1)
    assert (latest["symbol"], latest["asset_class"], latest["price"], latest["observed_at"]) == (
        "QQQ", "etf", 609.0, date(2026, 7, 10)
    )


def test_option_snapshot_is_narrow_deduplicated_partitioned_and_idempotent(
    repository: IngestionRepository,
    postgres_dsn: str,
) -> None:
    observed_at = datetime(2026, 7, 11, 8, 15, tzinfo=UTC)
    rows = [
        {
            "symbol": "NVDA",
            "expiry": "2026-08-21",
            "strike": 180,
            "type": "call",
            "contract_symbol": "rh-nvda-180c",
            "underlying_price": 175,
            "bid": 4.8,
            "ask": 5.2,
            "mid": 5.0,
            "volume": 120,
            "open_interest": 1500,
            "iv": 0.41,
            "delta": 0.43,
            "raw": {"large": "must not be copied into normalized quote facts"},
        },
        {
            "symbol": "NVDA",
            "expiry": "2026-08-21",
            "strike": 185,
            "type": "call",
            "contract_symbol": "rh-nvda-185c",
            "underlying_price": 175,
            "bid": 3.4,
            "ask": 3.8,
            "mid": 3.6,
            "volume": 90,
            "open_interest": 900,
            "iv": 0.4,
            "delta": 0.35,
        },
    ]
    run_id = repository.start_run("robinhood", "option_quotes", source_run_key="snapshot-1")
    first = repository.store_option_snapshot(
        run_id,
        source_id="robinhood",
        observed_at=observed_at,
        market_session="premarket",
        universe="owned+watchlist",
        rows=rows,
        completeness=1.0,
    )
    rows[0]["mid"] = 5.1
    second = repository.store_option_snapshot(
        run_id,
        source_id="robinhood",
        observed_at=observed_at,
        market_session="premarket",
        universe="owned+watchlist",
        rows=rows,
        completeness=1.0,
    )
    repository.finish_run(run_id, "succeeded")
    assert first == second

    with closing(psycopg.connect(postgres_dsn)) as connection:
        counts = connection.execute(
            "SELECT (SELECT count(*) FROM raw.option_snapshot), "
            "(SELECT count(*) FROM catalog.instrument), "
            "(SELECT count(*) FROM catalog.option_contract), "
            "(SELECT count(*) FROM raw.option_quote)"
        ).fetchone()
        quote = connection.execute(
            "SELECT q.mid, q.provider_iv, c.provider_symbols "
            "FROM raw.option_quote q JOIN catalog.option_contract c ON c.id = q.contract_id "
            "ORDER BY c.strike LIMIT 1"
        ).fetchone()
        columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'raw' AND table_name = 'option_quote'"
            ).fetchall()
        }
        partition = connection.execute(
            "SELECT to_regclass('raw.option_quote_202607')::text"
        ).fetchone()[0]
    assert counts == (1, 1, 2, 2)
    assert quote == (5.1, 0.41, {"robinhood": "rh-nvda-180c"})
    assert "raw" not in columns
    assert "ticker" not in columns
    assert "expiration" not in columns
    assert partition == "raw.option_quote_202607"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("observed_at", datetime(2026, 7, 11, 8, 15), "timezone-aware"),
        ("market_session", "lunch", "market_session"),
    ],
)
def test_option_snapshot_rejects_invalid_snapshot_metadata(
    repository: IngestionRepository,
    field: str,
    value: object,
    message: str,
) -> None:
    run_id = repository.start_run("robinhood", "option_quotes")
    arguments = {
        "source_id": "robinhood",
        "observed_at": datetime(2026, 7, 11, 8, 15, tzinfo=UTC),
        "market_session": "premarket",
        "universe": "watchlist",
        "rows": [],
    }
    arguments[field] = value
    with pytest.raises(ValueError, match=message):
        repository.store_option_snapshot(run_id, **arguments)


def test_robinhood_job_persists_collected_chain_to_postgresql(
    postgresql,
    postgres_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrade_database(postgres_dsn)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
database:
  url: {postgres_dsn}
nas:
  status_dir: {tmp_path / 'status'}
data_sources:
  brokers:
    enabled: true
    robinhood:
      enabled: true
      readonly: true
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        update_robinhood_options,
        "collect_robinhood_option_chains",
        lambda provider, symbols, client=None: {
            "rows": {
                "NVDA": [
                    {
                        "expiry": "2026-08-21",
                        "strike": 180,
                        "type": "call",
                        "contract_symbol": "rh-180c",
                        "bid": 4.8,
                        "ask": 5.2,
                        "mid": 5.0,
                        "open_interest": 1500,
                        "volume": 120,
                        "iv": 0.41,
                    }
                ]
            },
            "quotes": [
                {
                    "symbol": "NVDA",
                    "time": "2026-07-11T12:15:00Z",
                    "close": 175,
                    "change": 1.2,
                    "currency": "USD",
                }
            ],
            "market_data": "robinhood",
            "observed_at": "2026-07-11T12:15:00Z",
            "errors": [],
        },
    )

    result = update_robinhood_options.run(str(config_path), symbols=["NVDA"], client=object())

    assert result["status"] == "ok"
    assert result["chain_rows"] == 1
    assert result["database"] == "postgresql"
    with closing(psycopg.connect(postgres_dsn)) as connection:
        assert connection.execute("SELECT count(*) FROM raw.option_quote").fetchone()[0] == 1
        assert connection.execute("SELECT price FROM raw.quote").fetchone()[0] == 175
        assert connection.execute("SELECT status FROM ingest.run").fetchone()[0] == "succeeded"


def test_option_universe_honors_persisted_exclusion_over_config(repository: IngestionRepository) -> None:
    with repository.runtime.transaction() as connection:
        instrument = connection.execute(
            "INSERT INTO catalog.instrument (symbol, name, asset_class) "
            "VALUES ('PLTR', 'Palantir', 'equity') RETURNING id"
        ).fetchone()
        connection.execute(
            "INSERT INTO app.watchlist_item (instrument_id, watch_state) VALUES (%s, 'excluded')",
            [instrument["id"]],
        )

    universe = repository.option_universe([{"symbol": "PLTR"}, {"symbol": "NVDA"}])

    assert "PLTR" not in universe
    assert "NVDA" in universe


def test_option_universe_adds_upcoming_catalyst_discovery_candidate(repository: IngestionRepository) -> None:
    with repository.runtime.transaction() as connection:
        instrument = connection.execute(
            "INSERT INTO catalog.instrument (symbol, name, asset_class) "
            "VALUES ('RXRX', 'Recursion', 'equity') RETURNING id"
        ).fetchone()
        connection.execute(
            "INSERT INTO app.catalyst (instrument_id, starts_at, title) "
            "VALUES (%s, now() + interval '10 days', 'Clinical readout')",
            [instrument["id"]],
        )
        crypto = connection.execute(
            "INSERT INTO catalog.instrument (symbol, name, asset_class) "
            "VALUES ('BTC-USD', 'Bitcoin', 'crypto') RETURNING id"
        ).fetchone()
        connection.execute(
            "INSERT INTO app.catalyst (instrument_id, starts_at, title) "
            "VALUES (%s, now() + interval '5 days', 'Protocol event')",
            [crypto["id"]],
        )

    assert "RXRX" in repository.option_universe([])
    prioritized = repository.option_universe([{"symbol": "NVDA"}])
    assert prioritized[0] == "NVDA"
    assert "RXRX" in prioritized
    assert "BTC-USD" not in prioritized
