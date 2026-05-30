from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from investment_panel.core.config import load_config
from investment_panel.core.arco import flatten_arco_items, ingest_arco_theses, load_arco_context
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.research import build_research_packet, generate_deterministic_memo
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
                        "examples": [{"author": "tester", "url": "https://example.com/coin", "text": "$COIN thesis"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (arco_dir / "beliefs.json").write_text(json.dumps({"beliefs": []}), encoding="utf-8")
    (arco_dir / "birdclaw-bookmarks-2026-05-09.json").write_text(json.dumps({"canonicalItems": []}), encoding="utf-8")
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
    assert rows
    assert rows[0]["decision"] in {"monitor", "watch", "research"}


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
        symbols = query_rows(con, "SELECT symbol, source_url FROM birdclaw_theses ORDER BY symbol")

    assert rows == 3
    assert [row["symbol"] for row in symbols] == ["COIN", "NVDA", "TSLA"]
    assert {row["source_url"] for row in symbols} == {"https://x.com/analyst/status/bookmark-coin"}


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


def test_config_loads_paths(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
database:
  duckdb_path: data/test.duckdb
nas:
  status_dir: {tmp_path / "nas" / "status"}
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.database.duckdb_path.name == "test.duckdb"
    assert config.nas.status_dir == tmp_path / "nas" / "status"
