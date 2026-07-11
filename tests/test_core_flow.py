from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from investment_panel.core.config import load_config
from investment_panel.core.arco import (
    author_from_item,
    claims_from_item,
    flatten_arco_items,
    ingest_arco_theses,
    load_arco_context,
    source_url_from_item,
)
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.research import build_research_packet, generate_deterministic_memo
from investment_panel.core.source_ingestion.raw_sources import sync_private_raw_sources
from investment_panel.jobs.daily_screen import run as run_daily


def fixture_prices(symbol: str, lookback_days: int = 80, mode: str = "online") -> pd.DataFrame:
    rows = []
    for index in range(lookback_days):
        day = date(2026, 5, 20) - timedelta(days=lookback_days - index)
        if day.weekday() >= 5:
            continue
        close = 50.0 + index
        rows.append(
            {
                "symbol": symbol,
                "date": day,
                "open": close - 1,
                "high": close + 1,
                "low": close - 2,
                "close": close,
                "volume": 1_000_000.0 + index,
                "source": "test_fixture",
            }
        )
    return pd.DataFrame(rows)


@pytest.mark.slow
def test_daily_screen_builds_candidates_from_local_arco(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr("investment_panel.jobs.daily_screen.fetch_prices", fixture_prices)
    arco_dir = tmp_path / "arco"
    arco_dir.mkdir()
    (arco_dir / "signals.json").write_text(
        json.dumps(
            {
                "subtopics": [
                    {
                        "id": "ai-coinbase",
                        "topic": "Crypto Infrastructure",
                        "subtopic": "$COIN exchange infrastructure thesis",
                        "contrarianScore": 0.72,
                        "examples": [{"author": "tester", "url": "https://x.com/tester/status/333", "text": "$COIN thesis"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (arco_dir / "beliefs.json").write_text(json.dumps({"beliefs": []}), encoding="utf-8")
    (arco_dir / "birdclaw-bookmarks-2026-05-09.json").write_text(json.dumps({"canonicalItems": []}), encoding="utf-8")
    source_root = tmp_path / "nas"
    bird_data = source_root / "birdclaw-primary" / "exports" / "data"
    (bird_data / "tweets").mkdir(parents=True)
    (bird_data / "collections").mkdir(parents=True)
    (bird_data / "observations").mkdir(parents=True)
    (bird_data / "profiles.jsonl").write_text("", encoding="utf-8")
    write_jsonl(
        bird_data / "tweets" / "2026.jsonl",
        [
            {
                "id": "333",
                "created_at": "2026-06-01T10:00:00Z",
                "text": "$COIN raw tweet already interpreted by Arco.",
                "media_json": "[]",
                "entities_json": "{}",
            }
        ],
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
database:
  duckdb_path: {tmp_path / "investment.duckdb"}
nas:
  source_root: {tmp_path / "nas"}
  status_dir: {tmp_path / "nas" / "status"}
  market_dir: {tmp_path / "nas" / "market-mini"}
  duckdb_snapshot_dir: {tmp_path / "nas" / "market-mini" / "duckdb-snapshots"}
arco:
  raw_dir: {arco_dir}
market_data:
  mode: online
  lookback_days: 80
watchlist:
  - symbol: COIN
    name: Coinbase
    asset_class: equity
    category: crypto-infrastructure
""",
        encoding="utf-8",
    )

    result = run_daily(str(config_path))

    assert result["candidates"] >= 1
    assert Path(result["status_path"]).is_relative_to(tmp_path)
    assert Path(result["duckdb_snapshot"]).is_relative_to(tmp_path)
    with db(tmp_path / "investment.duckdb", read_only=True) as con:
        rows = query_rows(con, "SELECT symbol, score, decision FROM candidates WHERE symbol = 'COIN'")
        raw_items = query_rows(con, "SELECT id FROM source_items WHERE source_id = 'birdclaw_primary_tweets'")
        raw_signals = query_rows(con, "SELECT symbol FROM ticker_source_signals WHERE source_id = 'birdclaw_primary_tweets'")
    assert rows
    assert rows[0]["decision"] in {"monitor", "watch", "research"}
    assert raw_items == [{"id": "x_tweet:333"}]
    assert raw_signals == []
    assert result["raw_sources"]["analysis_overlap_items"] == 1


def test_arco_brief_ingest_enriches_from_selected_tweets_and_web_captures(tmp_path: Path) -> None:
    arco_dir = tmp_path / "brain" / "raw" / "sources" / "arco"
    arco_dir.mkdir(parents=True)
    (arco_dir / "brief-beliefs").mkdir()
    (arco_dir / "signals.json").write_text(json.dumps({"subtopics": []}), encoding="utf-8")
    (arco_dir / "beliefs.json").write_text(json.dumps({"beliefs": []}), encoding="utf-8")
    (arco_dir / "birdclaw-bookmarks-2026-05-10.json").write_text(
        json.dumps(
            {
                "canonicalItems": [
                    {
                        "id": "bookmark-coin",
                        "sourceType": "birdclaw_bookmark",
                        "exactText": "Saved $COIN infrastructure thesis",
                        "createdAt": "2026-05-10T10:00:00Z",
                        "author": {"handle": "analyst"},
                        "url": "https://x.com/analyst/status/bookmark-coin",
                    }
                ],
                "observedItems": [
                    {
                        "id": "observed-tsla",
                        "sourceType": "x_observed_tweet",
                        "exactText": "Read but not bookmarked $TSLA robotaxi risk discussion",
                        "createdAt": "2026-05-10T11:00:00Z",
                        "author": {"handle": "observer"},
                        "url": "https://x.com/observer/status/observed-tsla",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (arco_dir / "brief-beliefs" / "brief-beliefs-2026-05-10.json").write_text(
        json.dumps(
            {
                "schema": "arco.brief-beliefs.v1",
                "sourceBrief": "wiki/pages/arco/briefs/2026-05-10 Arco Intelligence Brief.md",
                "beliefs": [
                    {
                        "id": "market-brief",
                        "title": "Brief-selected market thesis",
                        "claim": "Arco brief selected these sources after summarizing exported X and web evidence.",
                        "confidence": "medium",
                        "evidence": [
                            {"author": "analyst", "url": "https://x.com/analyst/status/bookmark-coin"},
                            {"author": "observer", "url": "https://x.com/observer/status/observed-tsla"},
                            {"author": "web", "url": "https://example.com/nvda"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (arco_dir / "web-captures-2026-05-10.json").write_text(
        json.dumps(
            {
                "canonicalItems": [
                    {
                        "id": "web-nvda",
                        "sourceType": "web_capture",
                        "title": "$NVDA datacenter margins",
                        "pageText": "Saved web page about $NVDA datacenter margin pressure",
                        "capturedAt": "2026-05-10T12:00:00Z",
                        "url": "https://example.com/nvda",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (arco_dir / "source-manifest-2026-05-10.json").write_text(
        json.dumps(
            {
                "sourceSnapshots": [
                    {"sourceId": "birdclaw_bookmarks", "path": "raw/sources/arco/birdclaw-bookmarks-2026-05-10.json"},
                    {"sourceId": "browser_captures", "path": "raw/sources/arco/web-captures-2026-05-10.json"},
                ]
            }
        ),
        encoding="utf-8",
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
database:
  duckdb_path: {tmp_path / "investment.duckdb"}
arco:
  raw_dir: {arco_dir}
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    context = load_arco_context(config.arco)
    items = flatten_arco_items(context)

    assert [item["source_type"] for item in items] == ["arco_brief_belief"]
    assert "$COIN" in items[0]["text"]
    assert "$TSLA" in items[0]["text"]
    assert "$NVDA" in items[0]["text"]

    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        rows = ingest_arco_theses(con, context)
        symbols = query_rows(con, "SELECT symbol, author, source_url FROM birdclaw_theses ORDER BY symbol")

    assert rows == 3
    assert [row["symbol"] for row in symbols] == ["COIN", "NVDA", "TSLA"]
    assert {row["symbol"]: row["source_url"] for row in symbols} == {
        "COIN": "https://x.com/analyst/status/bookmark-coin",
        "NVDA": "https://example.com/nvda",
        "TSLA": "https://x.com/observer/status/observed-tsla",
    }
    assert {row["symbol"]: row["author"] for row in symbols} == {
        "COIN": "analyst",
        "NVDA": None,
        "TSLA": "observer",
    }


def test_arco_empty_replacement_does_not_delete_existing_theses(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            """
            INSERT INTO birdclaw_theses
            (id, symbol, author, created_at, thesis_summary, claims, engagement, source_url)
            VALUES ('existing', 'NVDA', 'arco', '2026-05-10T12:00:00Z', 'Existing thesis', '[]', '{}', 'https://example.com/existing')
            """
        )
        inserted = ingest_arco_theses(
            con,
            {
                "signals": {"subtopics": []},
                "beliefs": {"beliefs": []},
                "brief_beliefs": {"beliefs": []},
                "bookmarks": {"canonicalItems": []},
                "source_snapshots": [],
            },
        )
        rows = query_rows(con, "SELECT symbol, source_url FROM birdclaw_theses")

    assert inserted == 0
    assert rows == [{"symbol": "NVDA", "source_url": "https://example.com/existing"}]


def test_arco_brief_unmatched_symbol_does_not_use_first_evidence_url() -> None:
    item = {
        "id": "brief",
        "source_type": "arco_brief_belief",
        "title": "Aggregate thesis",
        "text": "$COIN and $NVDA both appear in the brief text.",
        "score": 0.65,
        "raw": {},
        "evidence": {
            "brief_evidence": [
                {"author": "coin", "url": "https://example.com/coin", "text": "$COIN source"},
                {"author": "nvda", "url": "https://example.com/nvda", "text": "NVIDIA source without cashtag"},
            ],
            "matched_source_items": [
                {"author": {"handle": "coin"}, "url": "https://example.com/coin", "text": "$COIN source"},
                {"author": {"handle": "nvda"}, "url": "https://example.com/nvda", "title": "NVIDIA source without cashtag"},
            ],
            "source_brief": "wiki/pages/arco/briefs/brief.md",
        },
    }

    assert source_url_from_item(item, "COIN") == "https://example.com/coin"
    assert author_from_item(item, "COIN") == "coin"
    assert source_url_from_item(item, "NVDA") == "wiki/pages/arco/briefs/brief.md"
    assert author_from_item(item, "NVDA") is None
    nvda_claims = claims_from_item(item, "NVDA")
    assert nvda_claims["evidence"] == {
        "source_brief": "wiki/pages/arco/briefs/brief.md",
        "validation_warnings": [],
        "unattributed_symbol": "NVDA",
    }


def test_arco_verified_empty_replacement_clears_existing_theses(tmp_path: Path) -> None:
    arco_dir = tmp_path / "arco"
    arco_dir.mkdir()
    (arco_dir / "signals.json").write_text(json.dumps({"subtopics": []}), encoding="utf-8")
    (arco_dir / "beliefs.json").write_text(json.dumps({"beliefs": []}), encoding="utf-8")
    (arco_dir / "brief-beliefs").mkdir()
    (arco_dir / "brief-beliefs" / "brief-beliefs-2026-05-10.json").write_text(
        json.dumps({"beliefs": []}),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
database:
  duckdb_path: {tmp_path / "investment.duckdb"}
arco:
  raw_dir: {arco_dir}
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    context = load_arco_context(config.arco)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        con.execute(
            """
            INSERT INTO birdclaw_theses
            (id, symbol, author, created_at, thesis_summary, claims, engagement, source_url)
            VALUES ('existing', 'NVDA', 'arco', '2026-05-10T12:00:00Z', 'Existing thesis', '[]', '{}', 'https://example.com/existing')
            """
        )
        inserted = ingest_arco_theses(con, context)
        rows = query_rows(con, "SELECT symbol, source_url FROM birdclaw_theses")

    assert inserted == 0
    assert rows == []


def test_private_raw_sources_materialize_and_dedup_arco_overlap(tmp_path: Path) -> None:
    source_root = tmp_path / "data-sources"
    bird_data = source_root / "birdclaw-primary" / "exports" / "data"
    (bird_data / "tweets").mkdir(parents=True)
    (bird_data / "collections").mkdir(parents=True)
    (bird_data / "observations").mkdir(parents=True)
    browser_captures = source_root / "browser-primary" / "captures"
    (browser_captures / "snapshots").mkdir(parents=True)
    (browser_captures / "events").mkdir(parents=True)

    write_jsonl(
        bird_data / "profiles.jsonl",
        [
            {
                "id": "profile_user_1",
                "handle": "analyst",
                "display_name": "Analyst",
                "followers_count": 1000,
            }
        ],
    )
    write_jsonl(
        bird_data / "tweets" / "2026.jsonl",
        [
            {
                "id": "111",
                "author_profile_id": "profile_user_1",
                "created_at": "2026-06-01T10:00:00Z",
                "text": "$NVDA $MSFT already cited by Arco analysis.",
                "bookmarked": 0,
                "liked": 0,
                "like_count": 10,
                "media_json": "[]",
                "entities_json": "{}",
            },
            {
                "id": "222",
                "author_profile_id": "profile_user_1",
                "created_at": "2026-06-02T10:00:00Z",
                "text": "$AMD raw tweet that Arco has not interpreted yet.",
                "bookmarked": 0,
                "liked": 0,
                "like_count": 20,
                "media_json": "[]",
                "entities_json": "{}",
            },
        ],
    )
    write_jsonl(
        bird_data / "collections" / "bookmarks.jsonl",
        [
            {
                "tweet_id": "111",
                "kind": "bookmarks",
                "updated_at": "2026-06-03T10:00:00Z",
                "raw_json": json.dumps(
                    {
                        "id": "111",
                        "text": "$NVDA $MSFT already cited by Arco analysis.",
                        "createdAt": "2026-06-01T10:00:00Z",
                        "author": {"username": "analyst", "name": "Analyst"},
                    }
                ),
            }
        ],
    )
    write_jsonl(
        bird_data / "observations" / "tweets.jsonl",
        [
            {
                "tweet_id": "222",
                "source": "chrome-extension",
                "surface": "home",
                "first_observed_at": "2026-06-03T12:00:00Z",
                "last_observed_at": "2026-06-03T12:05:00Z",
                "observed_date": "2026-06-03",
                "seen_count": 2,
                "raw_json": json.dumps(
                    {
                        "id": "222",
                        "url": "https://x.com/analyst/status/222",
                        "text": "$AMD raw tweet that Arco has not interpreted yet.",
                        "createdAt": "2026-06-02T10:00:00Z",
                        "observedAt": "2026-06-03T12:05:00Z",
                        "surface": "home",
                        "author": {"handle": "analyst", "displayName": "Analyst"},
                    }
                ),
            }
        ],
    )
    (browser_captures / "snapshots" / "web-captures-2026-06-03.json").write_text(
        json.dumps(
            {
                "canonicalItems": [
                    {
                        "id": "web-overlap",
                        "sourceUrl": "https://user:password@example.com/reset/secret-token?token=secret",
                        "title": (
                            "$NVDA $MSFT page already cited by Arco "
                            "https://user:password@example.com/reset/secret-token?token=secret"
                        ),
                        "pageText": "$NVDA $MSFT overlap",
                        "capturedAt": "2026-06-03T13:00:00Z",
                    },
                    {
                        "id": "https://example.com/id/secret-token?token=secret",
                        "url": "https://example.com/tsla/secret-token?token=secret",
                        "title": "$TSLA browser capture example.com/bare-token?token=secret",
                        "pageText": "$TSLA raw browser capture",
                        "author": {
                            "handle": "browser-source",
                            "displayName": "Browser Source",
                            "avatarUrl": "https://example.com/avatar/secret-token?token=secret",
                        },
                        "security": {"debugUrl": "https://example.com/security/secret-token?token=secret"},
                        "metadata": {"activeTabUrl": "https://example.com/tsla/secret-token?token=secret"},
                        "capturedAt": "2026-06-03T14:00:00Z",
                    },
                ],
                "observedItems": [
                    {
                        "id": "web-observed",
                        "url": "private-token-secret",
                        "pageText": "$COIN observed browser capture",
                        "capturedAt": "2026-06-03T15:00:00Z",
                    }
                ],
                "items": [
                    {
                        "id": "web-item",
                        "url": "https://example.com/neutral/secret-token?token=secret",
                        "pageText": "No ticker in this browser capture",
                        "capturedAt": "2026-06-03T16:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            """
            INSERT INTO birdclaw_theses
            (id, symbol, author, created_at, thesis_summary, claims, engagement, source_url)
            VALUES ('arco-nvda', 'NVDA', 'arco', '2026-06-03T00:00:00Z', 'Arco $NVDA thesis', ?, '{}', 'https://x.com/analyst/status/111')
            """,
            [
                json.dumps(
                    {"evidence": [{"url": "https://example.com/reset/secret-token"}, {"url": "https://example.com/status/222"}]}
                )
            ],
        )
        result = sync_private_raw_sources(con, source_root)
        items = query_rows(con, "SELECT source_id, id, url, summary, raw FROM source_items ORDER BY source_id, id")
        signals = query_rows(con, "SELECT source_id, symbol FROM ticker_source_signals ORDER BY source_id, symbol")
        signal_details = query_rows(
            con,
            """
            SELECT source_id, symbol, thesis, catalysts
            FROM ticker_source_signals
            ORDER BY source_id, symbol
            """,
        )
        runs = query_rows(con, "SELECT source_id, status, item_count, ticker_count FROM source_runs ORDER BY source_id")

    assert result["birdclaw_primary"]["raw_counts"] == {
        "tweets": 2,
        "bookmarks": 1,
        "observations": 1,
        "profiles": 1,
    }
    assert result["birdclaw_primary"]["items"] == 2
    assert result["browser_primary"]["raw_counts"] == {"snapshot_items": 4, "events": 0, "read_errors": 0}
    assert result["browser_primary"]["items"] == 4
    assert result["analysis_overlap_items"] == 2

    assert {(row["source_id"], row["id"]) for row in items if row["source_id"] == "birdclaw_primary_tweets"} == {
        ("birdclaw_primary_tweets", "x_tweet:111"),
        ("birdclaw_primary_tweets", "x_tweet:222"),
    }
    browser_item_ids = [row["id"] for row in items if row["source_id"] == "browser_primary_captures"]
    assert len(browser_item_ids) == 4
    assert all(item_id.startswith("web_capture:") for item_id in browser_item_ids)
    assert [(row["source_id"], row["symbol"]) for row in signals] == [
        ("birdclaw_primary_tweets", "AMD"),
        ("birdclaw_primary_tweets", "MSFT"),
        ("browser_primary_captures", "COIN"),
        ("browser_primary_captures", "MSFT"),
        ("browser_primary_captures", "TSLA"),
    ]
    raw_by_id = {row["id"]: json.loads(row["raw"]) for row in items}
    assert raw_by_id["x_tweet:111"]["analysis_overlap"] is True
    assert raw_by_id["x_tweet:111"]["analysis_overlap_symbols"] == ["NVDA"]
    assert raw_by_id["x_tweet:222"]["analysis_overlap"] is False
    assert raw_by_id["x_tweet:222"]["observation_surfaces"] == ["home"]
    assert all(json.loads(row["raw"]).get("redacted") is True for row in items if row["source_id"] == "birdclaw_primary_tweets")
    birdclaw_rows = [row for row in items if row["source_id"] == "birdclaw_primary_tweets"]
    birdclaw_item_text = json.dumps(birdclaw_rows, default=str)
    assert "$AMD raw tweet that Arco has not interpreted yet." not in birdclaw_item_text
    assert "$NVDA raw tweet already interpreted by Arco." not in birdclaw_item_text
    for raw in (raw_by_id["x_tweet:111"], raw_by_id["x_tweet:222"]):
        assert "text" not in raw
        assert "quotedTweet" not in raw
        assert "quoted_tweet" not in raw
        assert "media" not in raw
        assert "entities" not in raw
    browser_rows = [row for row in items if row["source_id"] == "browser_primary_captures"]
    assert not any("token=secret" in json.dumps(row, default=str) for row in browser_rows)
    assert not any("user:password" in json.dumps(row, default=str) for row in browser_rows)
    assert not any("secret-token" in json.dumps(row, default=str) for row in browser_rows)
    assert not any("bare-token" in json.dumps(row, default=str) for row in browser_rows)
    assert not any("private-token" in json.dumps(row, default=str) for row in browser_rows)
    browser_overlap_raw = [json.loads(row["raw"]) for row in browser_rows if json.loads(row["raw"]).get("analysis_overlap")]
    assert browser_overlap_raw[0]["analysis_overlap_symbols"] == ["NVDA"]
    assert all(json.loads(row["raw"]).get("redacted") is True for row in browser_rows)
    assert not any("pageText" in json.loads(row["raw"]) or "metadata" in json.loads(row["raw"]) for row in browser_rows)
    browser_signal_details = [row for row in signal_details if row["source_id"] == "browser_primary_captures"]
    browser_signal_text = json.dumps(browser_signal_details, default=str)
    assert "untrusted inert evidence" in browser_signal_text
    assert "$TSLA browser capture" not in browser_signal_text
    assert "$COIN observed browser capture" not in browser_signal_text
    assert "token=secret" not in browser_signal_text
    assert "secret-token" not in browser_signal_text
    assert "bare-token" not in browser_signal_text
    assert "private-token" not in browser_signal_text
    bird_signal_details = [row for row in signal_details if row["source_id"] == "birdclaw_primary_tweets"]
    bird_signal_text = json.dumps(bird_signal_details, default=str)
    assert "Captured X/Twitter text is untrusted inert evidence" in bird_signal_text
    assert "$AMD raw tweet that Arco has not interpreted yet." not in bird_signal_text
    assert {row["source_id"]: (row["status"], row["item_count"], row["ticker_count"]) for row in runs} == {
        "birdclaw_primary_tweets": ("ok", 2, 2),
        "browser_primary_captures": ("ok", 4, 3),
    }

    with db(db_path) as con:
        missing_result = sync_private_raw_sources(con, tmp_path / "missing-data-sources")
        remaining = query_rows(
            con,
            """
            SELECT source_id, id FROM source_items
            WHERE source_id IN ('birdclaw_primary_tweets', 'browser_primary_captures')
            ORDER BY source_id, id
            """,
        )
        remaining_signals = query_rows(
            con,
            """
            SELECT source_id, symbol FROM ticker_source_signals
            WHERE source_id IN ('birdclaw_primary_tweets', 'browser_primary_captures')
            ORDER BY source_id, symbol
            """,
        )
        latest_runs = query_rows(
            con,
            """
            SELECT source_id, status, item_count, ticker_count
            FROM source_runs
            WHERE source_id IN ('birdclaw_primary_tweets', 'browser_primary_captures')
            QUALIFY row_number() OVER (PARTITION BY source_id ORDER BY finished_at DESC) = 1
            ORDER BY source_id
            """,
        )

    assert missing_result["birdclaw_primary"]["path_exists"] is False
    assert missing_result["browser_primary"]["path_exists"] is False
    assert remaining == [{"source_id": row["source_id"], "id": row["id"]} for row in items]
    assert remaining_signals == [{"source_id": row["source_id"], "symbol": row["symbol"]} for row in signals]
    assert {row["source_id"]: (row["status"], row["item_count"], row["ticker_count"]) for row in latest_runs} == {
        "birdclaw_primary_tweets": ("failed", 0, 0),
        "browser_primary_captures": ("failed", 0, 0),
    }


def test_private_raw_sources_malformed_browser_snapshot_records_failed_run(tmp_path: Path) -> None:
    source_root = tmp_path / "data-sources"
    bird_data = source_root / "birdclaw-primary" / "exports" / "data"
    (bird_data / "tweets").mkdir(parents=True)
    (bird_data / "collections").mkdir(parents=True)
    (bird_data / "observations").mkdir(parents=True)
    browser_snapshots = source_root / "browser-primary" / "captures" / "snapshots"
    browser_events = source_root / "browser-primary" / "captures" / "events"
    browser_snapshots.mkdir(parents=True)
    browser_events.mkdir(parents=True)
    (bird_data / "profiles.jsonl").write_text("", encoding="utf-8")
    (browser_snapshots / "web-captures-2026-06-02.json").write_text(
        json.dumps(
            {
                "canonicalItems": [
                    {
                        "id": "web-good",
                        "url": "https://example.com/tsla",
                        "title": "$TSLA valid browser capture",
                        "capturedAt": "2026-06-02T12:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        first_result = sync_private_raw_sources(con, source_root)
        first_items = query_rows(
            con,
            """
            SELECT source_id, id FROM source_items
            WHERE source_id = 'browser_primary_captures'
            ORDER BY id
            """,
        )

    assert first_result["browser_primary"]["items"] == 1
    assert first_items

    (browser_snapshots / "web-captures-2026-06-03.json").write_text("{bad json", encoding="utf-8")
    with db(db_path) as con:
        result = sync_private_raw_sources(con, source_root)
        preserved_items = query_rows(
            con,
            """
            SELECT source_id, id FROM source_items
            WHERE source_id = 'browser_primary_captures'
            ORDER BY id
            """,
        )
        preserved_signals = query_rows(
            con,
            """
            SELECT source_id, symbol FROM ticker_source_signals
            WHERE source_id = 'browser_primary_captures'
            ORDER BY symbol
            """,
        )
        rows = query_rows(
            con,
            """
            SELECT source_id, status, item_count, ticker_count, failure_detail
            FROM source_runs
            WHERE source_id = 'browser_primary_captures'
            ORDER BY finished_at DESC
            LIMIT 1
            """,
        )

    assert result["browser_primary"]["raw_counts"] == {"snapshot_items": 1, "events": 0, "read_errors": 1}
    assert result["browser_primary"]["items"] == 0
    assert preserved_items == first_items
    assert preserved_signals == [{"source_id": "browser_primary_captures", "symbol": "TSLA"}]
    assert rows[0]["status"] == "failed"
    assert rows[0]["item_count"] == 0
    assert rows[0]["ticker_count"] == 0
    assert "malformed JSON" in rows[0]["failure_detail"]


def test_private_raw_sources_schema_invalid_browser_snapshot_records_failed_run(tmp_path: Path) -> None:
    source_root = tmp_path / "data-sources"
    bird_data = source_root / "birdclaw-primary" / "exports" / "data"
    (bird_data / "tweets").mkdir(parents=True)
    (bird_data / "collections").mkdir(parents=True)
    (bird_data / "observations").mkdir(parents=True)
    browser_snapshots = source_root / "browser-primary" / "captures" / "snapshots"
    browser_events = source_root / "browser-primary" / "captures" / "events"
    browser_snapshots.mkdir(parents=True)
    browser_events.mkdir(parents=True)
    (bird_data / "profiles.jsonl").write_text("", encoding="utf-8")
    (browser_snapshots / "web-captures-2026-06-02.json").write_text(
        json.dumps(
            {
                "canonicalItems": [
                    {
                        "id": "web-good",
                        "url": "https://example.com/tsla",
                        "title": "$TSLA valid browser capture",
                        "capturedAt": "2026-06-02T12:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        first_result = sync_private_raw_sources(con, source_root)
        first_items = query_rows(
            con,
            """
            SELECT source_id, id FROM source_items
            WHERE source_id = 'browser_primary_captures'
            ORDER BY id
            """,
        )

    assert first_result["browser_primary"]["items"] == 1
    assert first_items

    (browser_snapshots / "web-captures-2026-06-03.json").write_text(
        json.dumps({"canonicalItems": {"id": "not-a-list"}}),
        encoding="utf-8",
    )
    with db(db_path) as con:
        result = sync_private_raw_sources(con, source_root)
        preserved_items = query_rows(
            con,
            """
            SELECT source_id, id FROM source_items
            WHERE source_id = 'browser_primary_captures'
            ORDER BY id
            """,
        )
        rows = query_rows(
            con,
            """
            SELECT source_id, status, item_count, ticker_count, failure_detail
            FROM source_runs
            WHERE source_id = 'browser_primary_captures'
            ORDER BY finished_at DESC
            LIMIT 1
            """,
        )

    assert result["browser_primary"]["raw_counts"] == {"snapshot_items": 1, "events": 0, "read_errors": 1}
    assert result["browser_primary"]["items"] == 0
    assert preserved_items == first_items
    assert rows[0]["status"] == "failed"
    assert rows[0]["item_count"] == 0
    assert rows[0]["ticker_count"] == 0
    assert "canonicalItems is not a list" in rows[0]["failure_detail"]


def test_private_raw_sources_malformed_jsonl_preserves_last_good_rows(tmp_path: Path) -> None:
    source_root = tmp_path / "data-sources"
    bird_data = source_root / "birdclaw-primary" / "exports" / "data"
    (bird_data / "tweets").mkdir(parents=True)
    (bird_data / "collections").mkdir(parents=True)
    (bird_data / "observations").mkdir(parents=True)
    browser_captures = source_root / "browser-primary" / "captures"
    browser_snapshots = browser_captures / "snapshots"
    browser_events = browser_captures / "events"
    browser_snapshots.mkdir(parents=True)
    browser_events.mkdir(parents=True)
    (bird_data / "profiles.jsonl").write_text("", encoding="utf-8")
    tweet_row = {
        "id": "333",
        "created_at": "2026-06-04T10:00:00Z",
        "text": "$AMD valid row before a partial JSONL export",
        "media_json": "[]",
        "entities_json": "{}",
    }
    write_jsonl(bird_data / "tweets" / "2026.jsonl", [tweet_row])
    (browser_snapshots / "web-captures-2026-06-04.json").write_text(
        json.dumps(
            {
                "canonicalItems": [
                    {
                        "id": "web-bad-port",
                        "url": "https://example.com:bad/tsla/secret-token?token=secret",
                        "title": "$TSLA bad port https://example.com:bad/tsla/secret-token?token=secret",
                        "capturedAt": "2026-06-04T12:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        first_result = sync_private_raw_sources(con, source_root)
        first_items = query_rows(
            con,
            """
            SELECT source_id, id, url, summary, raw FROM source_items
            WHERE source_id IN ('birdclaw_primary_tweets', 'browser_primary_captures')
            ORDER BY source_id, id
            """,
        )
        first_signals = query_rows(
            con,
            """
            SELECT source_id, symbol FROM ticker_source_signals
            WHERE source_id IN ('birdclaw_primary_tweets', 'browser_primary_captures')
            ORDER BY source_id, symbol
            """,
        )

    assert first_result["birdclaw_primary"]["items"] == 1
    assert first_result["browser_primary"]["items"] == 1
    browser_rows = [row for row in first_items if row["source_id"] == "browser_primary_captures"]
    assert browser_rows[0]["url"] == "https://example.com"
    assert not any("secret-token" in json.dumps(row, default=str) for row in browser_rows)
    assert not any(":bad" in json.dumps(row, default=str) for row in browser_rows)

    (bird_data / "tweets" / "2026.jsonl").write_text(
        f"{json.dumps(tweet_row)}\n{{bad json\n",
        encoding="utf-8",
    )
    (browser_events / "events-2026-06-04.jsonl").write_text("{bad json\n", encoding="utf-8")
    with db(db_path) as con:
        result = sync_private_raw_sources(con, source_root)
        preserved_items = query_rows(
            con,
            """
            SELECT source_id, id, url, summary, raw FROM source_items
            WHERE source_id IN ('birdclaw_primary_tweets', 'browser_primary_captures')
            ORDER BY source_id, id
            """,
        )
        preserved_signals = query_rows(
            con,
            """
            SELECT source_id, symbol FROM ticker_source_signals
            WHERE source_id IN ('birdclaw_primary_tweets', 'browser_primary_captures')
            ORDER BY source_id, symbol
            """,
        )
        latest_runs = query_rows(
            con,
            """
            SELECT source_id, status, item_count, ticker_count, failure_detail
            FROM source_runs
            WHERE source_id IN ('birdclaw_primary_tweets', 'browser_primary_captures')
            QUALIFY row_number() OVER (PARTITION BY source_id ORDER BY finished_at DESC) = 1
            ORDER BY source_id
            """,
        )

    assert result["birdclaw_primary"]["raw_counts"] == {
        "tweets": 1,
        "bookmarks": 0,
        "observations": 0,
        "profiles": 0,
        "read_errors": 1,
    }
    assert result["browser_primary"]["raw_counts"] == {"snapshot_items": 1, "events": 0, "read_errors": 1}
    assert result["birdclaw_primary"]["items"] == 0
    assert result["browser_primary"]["items"] == 0
    assert preserved_items == first_items
    assert preserved_signals == first_signals
    assert {row["source_id"]: (row["status"], row["item_count"], row["ticker_count"]) for row in latest_runs} == {
        "birdclaw_primary_tweets": ("failed", 0, 0),
        "browser_primary_captures": ("failed", 0, 0),
    }
    assert all("malformed JSONL" in row["failure_detail"] for row in latest_runs)


def test_private_raw_sources_write_failure_preserves_last_good_rows(tmp_path: Path) -> None:
    source_root = tmp_path / "data-sources"
    bird_data = source_root / "birdclaw-primary" / "exports" / "data"
    (bird_data / "tweets").mkdir(parents=True)
    (bird_data / "collections").mkdir(parents=True)
    (bird_data / "observations").mkdir(parents=True)
    browser_snapshots = source_root / "browser-primary" / "captures" / "snapshots"
    browser_events = source_root / "browser-primary" / "captures" / "events"
    browser_snapshots.mkdir(parents=True)
    browser_events.mkdir(parents=True)
    (bird_data / "profiles.jsonl").write_text("", encoding="utf-8")
    write_jsonl(
        bird_data / "tweets" / "2026.jsonl",
        [
            {
                "id": "444",
                "created_at": "2026-06-05T10:00:00Z",
                "text": "$AMD valid row before a write-time failure",
                "media_json": "[]",
                "entities_json": "{}",
            }
        ],
    )
    (browser_snapshots / "web-captures-2026-06-05.json").write_text(
        json.dumps(
            {
                "canonicalItems": [
                    {
                        "id": "web-good",
                        "url": "https://example.com/tsla",
                        "title": "$TSLA valid browser capture",
                        "capturedAt": "2026-06-05T12:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        first_result = sync_private_raw_sources(con, source_root)
        first_items = query_rows(
            con,
            """
            SELECT source_id, id FROM source_items
            WHERE source_id IN ('birdclaw_primary_tweets', 'browser_primary_captures')
            ORDER BY source_id, id
            """,
        )
        first_signals = query_rows(
            con,
            """
            SELECT source_id, symbol FROM ticker_source_signals
            WHERE source_id IN ('birdclaw_primary_tweets', 'browser_primary_captures')
            ORDER BY source_id, symbol
            """,
        )

    assert first_result["birdclaw_primary"]["items"] == 1
    assert first_result["browser_primary"]["items"] == 1
    assert first_items
    assert first_signals

    write_jsonl(
        bird_data / "tweets" / "2026.jsonl",
        [
            {
                "id": "555",
                "created_at": "not-a-timestamp",
                "text": "$AMD malformed timestamp should not erase the prior import",
                "media_json": "[]",
                "entities_json": "{}",
            }
        ],
    )
    (browser_snapshots / "web-captures-2026-06-05.json").write_text(
        json.dumps(
            {
                "canonicalItems": [
                    {
                        "id": "web-bad-time",
                        "url": "https://example.com/tsla-bad-time",
                        "title": "$TSLA malformed timestamp should not erase the prior import",
                        "capturedAt": "not-a-timestamp",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with db(db_path) as con:
        result = sync_private_raw_sources(con, source_root)
        preserved_items = query_rows(
            con,
            """
            SELECT source_id, id FROM source_items
            WHERE source_id IN ('birdclaw_primary_tweets', 'browser_primary_captures')
            ORDER BY source_id, id
            """,
        )
        preserved_signals = query_rows(
            con,
            """
            SELECT source_id, symbol FROM ticker_source_signals
            WHERE source_id IN ('birdclaw_primary_tweets', 'browser_primary_captures')
            ORDER BY source_id, symbol
            """,
        )
        latest_runs = query_rows(
            con,
            """
            SELECT source_id, status, item_count, ticker_count, failure_detail
            FROM source_runs
            WHERE source_id IN ('birdclaw_primary_tweets', 'browser_primary_captures')
            QUALIFY row_number() OVER (PARTITION BY source_id ORDER BY finished_at DESC) = 1
            ORDER BY source_id
            """,
        )

    assert result["birdclaw_primary"]["items"] == 0
    assert result["browser_primary"]["items"] == 0
    assert preserved_items == first_items
    assert preserved_signals == first_signals
    assert {row["source_id"]: (row["status"], row["item_count"], row["ticker_count"]) for row in latest_runs} == {
        "birdclaw_primary_tweets": ("failed", 0, 0),
        "browser_primary_captures": ("failed", 0, 0),
    }
    assert all("source replacement failed" in row["failure_detail"] for row in latest_runs)


def test_research_packet_and_memo(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute("INSERT INTO instruments VALUES ('ABC', 'ABC Co', 'equity', NULL, NULL, 'test', 'test')")
        con.execute(
            "INSERT INTO candidates VALUES ('id1', current_date, 'ABC', 70, '{\"components\":{}}', '[]', 'watch')"
        )
        packet = build_research_packet(con, "ABC")
        memo = generate_deterministic_memo(packet)

    assert packet["symbol"] == "ABC"
    assert "# ABC Research Memo" in memo["markdown"]
    assert memo["json"]["decision"] == "watch"


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_config_loads_paths(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
database:
  url: postgresql://localhost/market_test
  duckdb_path: data/test.duckdb
nas:
  status_dir: {tmp_path / "nas" / "status"}
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.database.url == "postgresql://localhost/market_test"
    assert config.database.duckdb_path.name == "test.duckdb"
    assert config.nas.status_dir == tmp_path / "nas" / "status"


def test_config_loads_research_sources(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
research_sources:
  x:
    enabled: true
    list_id: "2066531259283656729"
    priority_handles: [balajis, karpathy]
    limit: 30
    account_fetch_cap: 1
  news:
    enabled: false
    providers: [reuters]
    limit: 12
  blogs:
    enabled: true
    substack_urls: ["https://example.com/feed"]
    rss_urls: []
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.research_sources.x.list_id == "2066531259283656729"
    assert config.research_sources.x.priority_handles == ["balajis", "karpathy"]
    assert config.research_sources.x.account_fetch_cap == 1
    assert config.research_sources.news.enabled is False
    assert config.research_sources.news.providers == ["reuters"]
    assert config.research_sources.blogs.substack_urls == ["https://example.com/feed"]


def test_config_allows_runtime_duckdb_override(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
database:
  duckdb_path: {tmp_path / "configured.duckdb"}
""",
        encoding="utf-8",
    )
    runtime_path = tmp_path / "runtime.duckdb"

    monkeypatch.setenv("MARKET_DUCKDB_PATH", str(runtime_path))

    config = load_config(path)

    assert config.database.duckdb_path == runtime_path


def test_config_allows_runtime_postgresql_override(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("database:\n  url: postgresql:///configured\n", encoding="utf-8")
    monkeypatch.setenv("MARKET_DATABASE_URL", "postgresql:///runtime")

    config = load_config(path)

    assert config.database.url == "postgresql:///runtime"
