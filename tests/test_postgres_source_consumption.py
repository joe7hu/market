from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from app.data_access.postgres_panel import load_postgres_tables
from app.data_access.user_state import thesis_monitor_rows
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.runtime import DatabaseRuntime


def _source(
    connection: psycopg.Connection,
    source_id: str,
    *,
    family: str = "research",
    name: str | None = None,
    kind: str = "article",
) -> str:
    connection.execute(
        "INSERT INTO ingest.source (id, name, family, kind, origin) "
        "VALUES (%s, %s, %s, %s, 'test')",
        [source_id, name or source_id.replace("-", " ").title(), family, kind],
    )
    return str(
        connection.execute(
            """
            INSERT INTO ingest.run
                (source_id, source_run_key, capability, started_at, finished_at, status)
            VALUES (%s, %s, 'content', now(), now(), 'succeeded')
            RETURNING id
            """,
            [source_id, f"run-{source_id}"],
        ).fetchone()[0]
    )


def _instrument(connection: psycopg.Connection, symbol: str, *, category: str = "content_reference") -> int:
    return int(
        connection.execute(
            """
            INSERT INTO catalog.instrument (symbol, name, asset_class, category)
            VALUES (%s, %s, 'equity', %s)
            ON CONFLICT (symbol) DO UPDATE SET updated_at = now()
            RETURNING id
            """,
            [symbol, symbol, category],
        ).fetchone()[0]
    )


def _content(
    connection: psycopg.Connection,
    *,
    source_id: str,
    run_id: str,
    source_key: str,
    instrument_ids: list[int],
    observed_at: datetime,
    title: str,
) -> int:
    item_id = int(
        connection.execute(
            """
            INSERT INTO raw.content_item
                (source_id, ingest_run_id, source_key, kind, title, url,
                 published_at, observed_at, summary)
            VALUES (%s, %s, %s, 'news', %s, %s, %s, %s, %s)
            RETURNING id
            """,
            [source_id, run_id, source_key, title, f"https://example.test/{source_key}", observed_at, observed_at, title],
        ).fetchone()[0]
    )
    for instrument_id in instrument_ids:
        connection.execute(
            "INSERT INTO raw.content_item_instrument (content_item_id, instrument_id, relevance) "
            "VALUES (%s, %s, 0.8)",
            [item_id, instrument_id],
        )
    return item_id


def test_feed_balances_sources_groups_tickers_and_excludes_future_rows(migrated_postgres_dsn: str) -> None:
    now = datetime.now(UTC)
    with psycopg.connect(migrated_postgres_dsn) as connection:
        crowded_run = _source(connection, "crowded-wire", kind="news")
        crowded_alias_run = _source(
            connection, "crowded-wire-alias", name="CROWDED WIRE", kind="news",
        )
        second_run = _source(connection, "second-wire")
        future_run = _source(connection, "future-calendar", family="estimates")
        nvda = _instrument(connection, "NVDA")
        tsla = _instrument(connection, "TSLA")
        for index in range(60):
            _content(
                connection, source_id="crowded-wire", run_id=crowded_run,
                source_key=f"crowded-{index}", instrument_ids=[nvda],
                observed_at=now - timedelta(minutes=index), title=f"NVDA crowded update {index}",
            )
        _content(
            connection, source_id="crowded-wire-alias", run_id=crowded_alias_run,
            source_key="crowded-alias", instrument_ids=[nvda],
            observed_at=now - timedelta(seconds=30), title="NVDA crowded alias update",
        )
        grouped_id = _content(
            connection, source_id="second-wire", run_id=second_run,
            source_key="second-grouped", instrument_ids=[nvda, tsla],
            observed_at=now - timedelta(minutes=2), title="NVDA and TSLA shared supply-chain update",
        )
        for index in range(60):
            _content(
                connection, source_id="future-calendar", run_id=future_run,
                source_key=f"future-{index}", instrument_ids=[nvda],
                observed_at=now + timedelta(days=index + 1), title=f"Future event {index}",
            )

    tables, _ = load_postgres_tables({"database": {"url": migrated_postgres_dsn}}, ("feed_signals",))
    rows = tables["feed_signals"]

    assert {row["source"] for row in rows} == {"crowded wire", "Second Wire"}
    assert all(datetime.fromisoformat(str(row["date"])) <= now for row in rows)
    grouped = [row for row in rows if row["id"] == f"content:{grouped_id}"]
    assert len(grouped) == 1
    assert grouped[0]["symbols"] == ["NVDA", "TSLA"]
    assert sum(row["source"].lower() == "crowded wire" for row in rows) <= 8


def test_source_evidence_promotes_canonical_candidates_into_both_universe_models(
    migrated_postgres_dsn: str,
) -> None:
    now = datetime.now(UTC)
    with psycopg.connect(migrated_postgres_dsn) as connection:
        source_run = _source(connection, "candidate-wire")
        corroborating_run = _source(connection, "candidate-wire-2")
        pltr = _instrument(connection, "PLTR", category="market_data")
        malformed_pltr = _instrument(connection, "PLTR.")
        thin = _instrument(connection, "THIN")
        future = _instrument(connection, "FUTR")
        _content(
            connection, source_id="candidate-wire", run_id=source_run,
            source_key="pltr-evidence", instrument_ids=[malformed_pltr],
            observed_at=now - timedelta(hours=1), title="PLTR contract evidence",
        )
        _content(
            connection, source_id="candidate-wire-2", run_id=corroborating_run,
            source_key="pltr-corroboration", instrument_ids=[pltr],
            observed_at=now - timedelta(hours=2), title="PLTR corroborating evidence",
        )
        _content(
            connection, source_id="candidate-wire", run_id=source_run,
            source_key="thin-evidence", instrument_ids=[thin],
            observed_at=now - timedelta(hours=3), title="THIN single-source evidence",
        )
        _content(
            connection, source_id="candidate-wire", run_id=source_run,
            source_key="future-only", instrument_ids=[future],
            observed_at=now + timedelta(days=30), title="Future-only ticker event",
        )
        assert pltr != malformed_pltr

    tables, _ = load_postgres_tables(
        {"database": {"url": migrated_postgres_dsn}},
        ("discovered_universe", "universe_screen"),
    )

    for model in ("discovered_universe", "universe_screen"):
        rows = {row["symbol"]: row for row in tables[model]}
        assert "PLTR" in rows
        assert "PLTR." not in rows
        assert "FUTR" not in rows
        assert rows["PLTR"]["source_count"] == 2
        assert "candidate-wire" in rows["PLTR"]["source_counts"]
        assert rows["PLTR"]["watch_state"] == "candidate"
        assert rows["THIN"]["source_count"] == 1

    discovered = {row["symbol"]: row for row in tables["discovered_universe"]}
    assert discovered["PLTR"]["eligibility_status"] == "eligible"
    assert discovered["PLTR"]["decision_universe_member"] is True
    assert discovered["THIN"]["eligibility_status"] == "source_thin"
    assert discovered["THIN"]["decision_universe_member"] is False

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        option_universe = IngestionRepository(runtime).option_universe([])
    finally:
        runtime.close()
    assert "PLTR" in option_universe
    assert "PLTR." not in option_universe
    assert "FUTR" not in option_universe


def test_thesis_monitor_consumes_diverse_source_evidence_without_replacing_user_thesis(
    migrated_postgres_dsn: str,
) -> None:
    now = datetime.now(UTC)
    with psycopg.connect(migrated_postgres_dsn) as connection:
        first_run = _source(connection, "thesis-wire")
        second_run = _source(connection, "thesis-social", family="social")
        nvda = _instrument(connection, "NVDA", category="watchlist")
        connection.execute(
            "INSERT INTO app.watchlist_item (instrument_id, watch_state) VALUES (%s, 'watched')", [nvda]
        )
        connection.execute(
            """
            INSERT INTO app.thesis (instrument_id, revision, status, thesis, updated_at)
            VALUES (%s, 1, 'current', %s, %s)
            """,
            [
                nvda,
                Jsonb({
                    "core_thesis": "User-authored AI infrastructure thesis.",
                    "why_owned_watched": "Durable accelerator demand.",
                    "invalidation": "Datacenter demand rolls over.",
                    "last_reviewed": (now - timedelta(days=2)).isoformat(),
                }),
                now - timedelta(days=2),
            ],
        )
        _content(
            connection, source_id="thesis-wire", run_id=first_run,
            source_key="nvda-wire", instrument_ids=[nvda],
            observed_at=now - timedelta(hours=2), title="NVDA demand update",
        )
        _content(
            connection, source_id="thesis-social", run_id=second_run,
            source_key="nvda-social", instrument_ids=[nvda],
            observed_at=now - timedelta(hours=1), title="NVDA supply-chain discussion",
        )

    row = thesis_monitor_rows({"database": {"url": migrated_postgres_dsn}})[0]

    assert row["thesis"] == "User-authored AI infrastructure thesis."
    assert set(row["source_names"]) == {"Thesis Social", "Thesis Wire"}
    assert row["source_count"] == 2
    assert row["source_evidence_count"] == 2
    assert row["evidence_newer_than_review"] is True
    assert row["needs_review"] is True
    assert "new source evidence" in row["review_reason"]
    assert {"https://example.test/nvda-wire", "https://example.test/nvda-social"}.issubset(
        set(row["evidence_links"])
    )
