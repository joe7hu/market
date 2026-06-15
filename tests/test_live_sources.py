"""Live opencli ingestion: normalization, signals, source_runs, rate limits."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from investment_panel.core.db import db, init_db, query_rows, upsert_instrument
from investment_panel.providers.opencli import OpenCliRateLimitError
from investment_panel.core.source_ingestion.live import (
    fetch_news,
    fetch_substack,
    fetch_web_rss,
    fetch_x_account,
    fetch_x_list,
    known_symbols,
)


class FakeRunner:
    """Stand-in for OpenCliRunner: returns canned JSON or raises."""

    def __init__(self, payload: Any = None, *, raises: Exception | None = None) -> None:
        self.payload = payload
        self.raises = raises
        self.calls: list[list[str]] = []

    def read_json(self, args: list[str]) -> Any:
        self.calls.append(args)
        if self.raises is not None:
            raise self.raises
        return self.payload


def _con(tmp_path: Path):
    db_path = tmp_path / "live.duckdb"
    init_db(db_path)
    return db(db_path, read_only=False)


def test_fetch_x_list_normalizes_tweets_and_records_run(tmp_path: Path) -> None:
    tweets = [
        {
            "id": "111",
            "author": "balajis",
            "name": "Balaji",
            "text": "Bullish on $NVDA and $TSM this cycle",
            "likes": 10,
            "retweets": 2,
            "created_at": "2026-06-14T12:00:00Z",
            "url": "https://x.com/balajis/status/111",
        }
    ]
    runner = FakeRunner(tweets)
    with _con(tmp_path) as con:
        result = fetch_x_list(con, runner, "list-123", limit=5)
        items = query_rows(con, "SELECT id, source_kind, tickers FROM source_items WHERE id = 'x_tweet:111'")
        signals = query_rows(con, "SELECT symbol FROM ticker_source_signals WHERE source_item_id = 'x_tweet:111'")
        runs = query_rows(con, "SELECT status, capability FROM source_runs")

    assert result.status == "ok"
    assert result.items == 1
    assert len(items) == 1
    symbols = {row.get("symbol") for row in signals}
    assert {"NVDA", "TSM"} <= symbols
    assert any(run.get("capability") == "x_list" and run.get("status") == "ok" for run in runs)


def test_fetch_x_list_dedupes_existing_tweets(tmp_path: Path) -> None:
    tweets = [{"id": "222", "author": "karpathy", "text": "$AMD note", "created_at": "2026-06-14T12:00:00Z"}]
    with _con(tmp_path) as con:
        first = fetch_x_list(con, FakeRunner(tweets), "list-1", limit=5)
        second = fetch_x_list(con, FakeRunner(tweets), "list-1", limit=5)
    assert first.items == 1
    assert second.items == 0
    assert second.skipped == 1


def test_fetch_x_list_empty_list_id_is_skipped(tmp_path: Path) -> None:
    runner = FakeRunner([])
    with _con(tmp_path) as con:
        result = fetch_x_list(con, runner, "", limit=5)
    assert result.status == "skipped"
    assert runner.calls == []


def test_fetch_x_account_rate_limited_records_run_and_does_not_crash(tmp_path: Path) -> None:
    runner = FakeRunner(raises=OpenCliRateLimitError("scanner 429: too many requests"))
    with _con(tmp_path) as con:
        result = fetch_x_account(con, runner, "balajis", limit=5)
        runs = query_rows(con, "SELECT status FROM source_runs")
    assert result.rate_limited is True
    assert result.status == "rate_limited"
    assert any(run.get("status") == "rate_limited" for run in runs)


def test_fetch_news_normalizes_and_extracts_cashtags(tmp_path: Path) -> None:
    rows = [
        {"title": "$AAPL beats on services revenue", "link": "https://news/aapl", "summary": "strong quarter"},
        {"title": "Markets steady ahead of CPI", "link": "https://news/cpi"},
    ]
    runner = FakeRunner(rows)
    with _con(tmp_path) as con:
        result = fetch_news(con, runner, "bloomberg", limit=10)
        news = query_rows(con, "SELECT title FROM news_items")
        signals = query_rows(con, "SELECT symbol FROM ticker_source_signals")
        runs = query_rows(con, "SELECT status, source_id FROM source_runs")
    assert result.status == "ok"
    assert len(news) == 2
    assert any(row.get("symbol") == "AAPL" for row in signals)
    assert any(run.get("status") == "ok" for run in runs)


def test_fetch_news_unknown_provider_skips(tmp_path: Path) -> None:
    runner = FakeRunner([])
    with _con(tmp_path) as con:
        result = fetch_news(con, runner, "not-a-provider")
    assert result.status == "skipped"
    assert runner.calls == []


def test_fetch_substack_creates_blog_source_items(tmp_path: Path) -> None:
    posts = [{"title": "AI capex and $MSFT", "link": "https://sub.stack/post1", "summary": "memo"}]
    runner = FakeRunner(posts)
    with _con(tmp_path) as con:
        result = fetch_substack(con, runner, "https://example.substack.com")
        items = query_rows(con, "SELECT id, source_kind FROM source_items WHERE source_kind = 'blog'")
        signals = query_rows(con, "SELECT symbol FROM ticker_source_signals")
    assert result.status == "ok"
    assert len(items) == 1
    assert any(row.get("symbol") == "MSFT" for row in signals)


def test_fetch_web_rss_creates_blog_source_items(tmp_path: Path, monkeypatch) -> None:
    feed = b"""<?xml version="1.0"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>AI chips and $NVDA</title>
          <link>https://example.com/post</link>
          <description>Supply chain note</description>
          <pubDate>Mon, 15 Jun 2026 12:00:00 GMT</pubDate>
          <dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">Analyst</dc:creator>
        </item>
      </channel>
    </rss>"""

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return feed

    monkeypatch.setattr("investment_panel.core.source_ingestion.live.blog_sources.urlopen", lambda *_args, **_kwargs: FakeResponse())
    with _con(tmp_path) as con:
        result = fetch_web_rss(con, FakeRunner(), "https://example.com/feed")
        items = query_rows(con, "SELECT title, source_kind FROM source_items WHERE source_kind = 'blog'")
        signals = query_rows(con, "SELECT symbol FROM ticker_source_signals")
        runs = query_rows(con, "SELECT status, capability FROM source_runs")

    assert result.status == "ok"
    assert result.items == 1
    assert items == [{"title": "AI chips and $NVDA", "source_kind": "blog"}]
    assert any(row.get("symbol") == "NVDA" for row in signals)
    assert any(run.get("capability") == "rss" and run.get("status") == "ok" for run in runs)


def test_known_symbols_enables_bare_mention_extraction(tmp_path: Path) -> None:
    rows = [{"title": "PLTR contract win expands backlog", "link": "https://news/pltr"}]
    runner = FakeRunner(rows)
    with _con(tmp_path) as con:
        upsert_instrument(con, {"symbol": "PLTR", "name": "Palantir", "asset_class": "equity", "category": "test"})
        known = known_symbols(con)
        fetch_news(con, runner, "reuters", limit=5, known=known)
        signals = query_rows(con, "SELECT symbol FROM ticker_source_signals")
    assert any(row.get("symbol") == "PLTR" for row in signals)
