from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.jobs import update_content_sources


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
                "ON CONFLICT (symbol) DO NOTHING"
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
                return [{"id": "story-1", "title": "$NVDA launches a new platform", "url": "https://example.test/1"}]

        monkeypatch.setattr(update_content_sources, "load_config", lambda _path=None: config)
        monkeypatch.setattr(update_content_sources, "runtime_for_config", lambda _config: runtime)
        monkeypatch.setattr(update_content_sources, "OpenCliRunner", lambda **_kwargs: _Runner())

        result = update_content_sources.run("config.yaml", kinds={"news"})

        assert result["status"] == "ok"
        assert result["items"] == 1
        assert result["instrument_links"] == 1
        assert result["affected_symbols"] == ["NVDA"]
        assert result["signals"] == 1

        unchanged = update_content_sources.run("config.yaml", kinds={"news"})
        assert unchanged["affected_symbols"] == []
        assert unchanged["signals"] == 0
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
        assert row["title"] == "$NVDA launches a new platform"
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
    finally:
        runtime.close()
