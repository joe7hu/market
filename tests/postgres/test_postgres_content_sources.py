from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.jobs import update_content_sources
from investment_panel.infrastructure.providers.opencli import OpenCliUnavailableError


def test_content_refresh_archives_payload_and_stores_compact_linked_facts(
    migrated_postgres_dsn: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('NVDA', 'NVIDIA', 'equity') "
                "ON CONFLICT (symbol) DO UPDATE SET name = EXCLUDED.name"
            )
        config = SimpleNamespace(
            database=SimpleNamespace(url=migrated_postgres_dsn),
            nas=SimpleNamespace(market_dir=tmp_path / "nas"),
            report_dir=tmp_path / "reports",
            data_sources=SimpleNamespace(opencli=SimpleNamespace(command="opencli", timeout_seconds=1)),
            research_sources=SimpleNamespace(
                news=SimpleNamespace(enabled=True, providers=["hackernews"], limit=10),
                blogs=SimpleNamespace(enabled=False, substack_urls=[], rss_urls=[]),
                x=SimpleNamespace(enabled=False, list_id="", limit=10),
            ),
        )

        class _Runner:
            def read_json(self, _args):
                return [{"id": "story-1", "title": "NVIDIA launches a new platform", "url": "https://example.test/1"}]

        monkeypatch.setattr(update_content_sources, "load_config", lambda _path=None: config)
        monkeypatch.setattr(update_content_sources, "runtime_for_config", lambda _config: runtime)
        monkeypatch.setattr(update_content_sources, "OpenCliRunner", lambda **_kwargs: _Runner())

        result = update_content_sources.run("config.yaml", kinds={"news"})

        assert result["status"] == "ok"
        assert result["items"] == 1
        assert result["instrument_links"] == 1
        assert result["affected_symbols"] == ["NVDA"]
        assert result["signals"] == 1
        assert result["source_status"] == "ok"
        assert result["linking_status"] == "ok"
        assert result["publication_status"] == "published"

        unchanged = update_content_sources.run("config.yaml", kinds={"news"})
        assert unchanged["affected_symbols"] == []
        assert unchanged["signals"] == 0
        assert unchanged["linking_status"] == "ok"
        assert unchanged["publication_status"] == "unchanged"
        with runtime.read() as connection:
            row = connection.execute(
                """
                SELECT item.title, item.metadata, instrument.symbol, payload.archive_uri,
                       run.status
                FROM raw.content_item item
                JOIN raw.content_item_instrument link ON link.content_item_id = item.id
                JOIN catalog.instrument instrument ON instrument.id = link.instrument_id
                JOIN ingest.payload payload ON payload.id = item.payload_id
                JOIN ingest.run run ON run.id = item.ingest_run_id
                """
            ).fetchone()
            signal = connection.execute(
                """
                SELECT signal.signal_type, signal.sentiment, signal.direction,
                       signal.details, run.status AS analysis_status
                FROM analysis.source_signal signal
                JOIN analysis.run run ON run.id = signal.run_id
                JOIN raw.content_item item ON item.id = signal.content_item_id
                WHERE item.source_id = 'news_hackernews'
                ORDER BY signal.id DESC
                LIMIT 1
                """
            ).fetchone()
        assert row["title"] == "NVIDIA launches a new platform"
        assert row["metadata"] == {"provider": "news_hackernews"}
        assert row["symbol"] == "NVDA"
        assert row["status"] == "succeeded"
        assert Path(str(row["archive_uri"]).removeprefix("file://")).is_file()
        assert signal["signal_type"] == "content-hypothesis-v1"
        assert signal["sentiment"] == "neutral"
        assert signal["direction"] == "NEUTRAL"
        assert signal["details"]["evidence_state"] == "HYPOTHESIS"
        assert signal["details"]["transformation"] == "content-hypothesis-v1"
        assert signal["analysis_status"] == "succeeded"
        monkeypatch.setattr(_Runner, "read_json", lambda _self, _args: [{"id": "macro-1", "title": "Markets close for a holiday"}])
        unlinked = update_content_sources.run("config.yaml", kinds={"news"})
        assert unlinked["source_status"] == "ok"
        assert unlinked["linking_status"] == "unmatched"
        assert unlinked["publication_status"] == "no_links"
        assert unlinked["items"] == 1 and unlinked["signals"] == 0
    finally:
        runtime.close()


def test_social_access_timeout_is_partial_and_does_not_publish_thesis(
    migrated_postgres_dsn: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        config = SimpleNamespace(
            database=SimpleNamespace(url=migrated_postgres_dsn),
            nas=SimpleNamespace(market_dir=tmp_path / "nas"),
            report_dir=tmp_path / "reports",
            data_sources=SimpleNamespace(opencli=SimpleNamespace(command="opencli", timeout_seconds=1)),
            research_sources=SimpleNamespace(
                news=SimpleNamespace(enabled=False, providers=[], limit=10),
                blogs=SimpleNamespace(enabled=False, substack_urls=[], rss_urls=[]),
                x=SimpleNamespace(enabled=True, list_id="list-1", limit=10),
            ),
        )

        class _Runner:
            def read_json(self, _args):
                raise OpenCliUnavailableError("OpenCLI timed out")

        monkeypatch.setattr(update_content_sources, "load_config", lambda _path=None: config)
        monkeypatch.setattr(update_content_sources, "runtime_for_config", lambda _config: runtime)
        monkeypatch.setattr(update_content_sources, "OpenCliRunner", lambda **_kwargs: _Runner())

        result = update_content_sources.run("config.yaml", kinds={"social"})

        assert result["status"] == "partial"
        assert result["runs"][0]["status"] == "unavailable"
        assert result["runs"][0]["downstream_status"] == "not_run"
        assert result["source_status"] == "partial"
        assert result["linking_status"] == "not_run"
        assert result["publication_status"] == "not_run"
        with runtime.read() as connection:
            row = connection.execute(
                "SELECT status, failure_detail FROM ingest.run WHERE source_id = 'birdclaw_primary_tweets' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row["status"] == "partial"
        assert row["failure_detail"] == "OpenCLI timed out"
    finally:
        runtime.close()


def test_content_links_provider_identifiers_and_keeps_catalog_collisions_unresolved(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            for symbol, name in (("NVDA", "NVIDIA Corporation"), ("GOOG", "Alphabet Inc."), ("GOOGL", "Alphabet Inc.")):
                connection.execute(
                    "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES (%s, %s, 'equity') "
                    "ON CONFLICT (symbol) DO UPDATE SET name = EXCLUDED.name",
                    [symbol, name],
                )
            connection.execute(
                "INSERT INTO catalog.instrument_alias (instrument_id, provider, external_symbol, exchange) "
                "SELECT id, 'reuters', 'NVDA.O', 'NASDAQ' FROM catalog.instrument WHERE symbol = 'NVDA'"
            )
            connection.execute(
                "INSERT INTO catalog.instrument_alias (instrument_id, provider, external_symbol, exchange) "
                "SELECT id, 'reuters', 'ALPHABET', symbol FROM catalog.instrument WHERE symbol IN ('GOOG', 'GOOGL')"
            )
        known, names, aliases = update_content_sources._content_catalog(runtime)
        row = update_content_sources._content_row(
            "news_reuters", "news",
            {"title": "Chip maker reports its quarterly result", "instruments": [{"ric": "NVDA.O", "exchange": "NASDAQ"}]},
            known, company_names=names, provider_aliases=aliases, provider="reuters",
        )
        assert row is not None and row["symbols"] == ["NVDA"]
        assert update_content_sources._content_signal_rows("news_reuters", [row])[0]["direction"] == "NEUTRAL"
        for title, instruments, reason in (
            ("Alphabet releases a product", [], "ambiguous_company_name"),
            ("Share classes trade actively", ["ALPHABET"], "ambiguous_provider_identifier"),
            ("Chip maker reports", [{"ric": "NVDA.O", "exchange": "NYSE"}], "unresolved_provider_identifier"),
        ):
            row = update_content_sources._content_row(
                "news_reuters", "news", {"title": title, "instruments": instruments}, known,
                company_names=names, provider_aliases=aliases, provider="reuters",
            )
            assert row is not None and row["symbols"] == []
            assert row["metadata"]["unresolved_instrument_references"][0]["reason"] == reason
        other_provider = update_content_sources._content_row(
            "news_bloomberg", "news", {"title": "Chip maker reports", "instruments": ["NVDA.O"]}, known,
            company_names=names, provider_aliases=aliases, provider="bloomberg",
        )
        assert other_provider is not None and other_provider["symbols"] == []
    finally:
        runtime.close()


def test_content_explicit_symbols_do_not_require_directional_language() -> None:
    row = update_content_sources._content_row(
        "birdclaw_primary_tweets", "social",
        {"title": "A discussion of $F and technology", "symbols": ["msft"], "entities": {"symbols": [{"text": "nvda"}]}},
        {"F", "MSFT", "NVDA"},
    )
    assert row is not None and row["symbols"] == ["F", "MSFT", "NVDA"]
    signals = update_content_sources._content_signal_rows("birdclaw_primary_tweets", [row])
    assert all(signal["direction"] == "NEUTRAL" for signal in signals)
    assert all(signal["details"]["available_at"] == row["observed_at"].isoformat() for signal in signals)
