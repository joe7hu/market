"""Source registry, canonical source items, and source health checks."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from investment_panel.core.db import json_dumps, query_rows, upsert_instrument
from investment_panel.core.instruments import infer_asset_class, normalize_symbol


VERIFIED_SOURCES = [
    {
        "source": "sec_edgar",
        "source_url": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
        "detail": "Free server-side JSON APIs; use declared user-agent; SEC fair-access max is 10 requests/sec total.",
    },
    {
        "source": "sec_13f",
        "source_url": "https://www.sec.gov/files/form_13f.pdf",
        "detail": "Quarterly official ZIP datasets from May 2013 forward; tab-delimited flat files; as-filed caveats apply.",
    },
    {
        "source": "coingecko",
        "source_url": "https://docs.coingecko.com/reference/coins-markets",
        "detail": "Demo/free REST API uses api.coingecko.com; 30 calls/min and 10k/month; categories update about every 5 minutes.",
    },
    {
        "source": "defillama",
        "source_url": "https://defillama.com/docs/api",
        "detail": "Free unauthenticated API for protocols, TVL, fees/revenue; yields currently use yields.llama.fi.",
    },
    {
        "source": "yfinance",
        "source_url": "https://pypi.org/project/yfinance/",
        "detail": "Unofficial Yahoo Finance wrapper intended for research/education and personal use; cache/fallback required.",
    },
    {
        "source": "stooq",
        "source_url": "https://pydata.github.io/pandas-datareader/readers/stooq.html",
        "detail": "Available through pandas-datareader StooqDailyReader; website-backed, not a contracted API.",
    },
    {
        "source": "opencli",
        "source_url": "https://github.com/jackwener/opencli",
        "detail": "Local CLI adapter registry used read-only for research sources; Market allowlists commands through provider adapters.",
    },
    {
        "source": "tradingview_opencli",
        "source_url": "https://github.com/himself65/finance-skills/tree/main/opencli-plugins/tradingview",
        "detail": "Read-only TradingView desktop adapter for quotes, screeners, news, watchlists, alerts, chart state, and options chains.",
    },
]

SOURCE_DEFINITIONS = [
    {
        "source_id": "sec_edgar",
        "source_name": "SEC EDGAR",
        "source_family": "filing",
        "source_kind": "sec_api",
        "origin": "market",
        "enabled": True,
        "ingestion_mode": "direct_api",
        "raw_access": "free_public",
        "source_url": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
        "notes": "Primary SEC company filings API; includes 8-K, 10-Q, 10-K, DEF 14A, 6-K, and Form 4 targets.",
    },
    {
        "source_id": "sec_disclosures",
        "source_name": "SEC disclosures",
        "source_family": "filing",
        "source_kind": "disclosure",
        "origin": "market",
        "enabled": True,
        "ingestion_mode": "direct_api",
        "raw_access": "free_public",
        "source_url": "https://www.sec.gov/edgar/search/",
        "notes": "Canonical bucket for 13F/Form 4/PTR-style disclosure signals already stored in Market.",
    },
    {
        "source_id": "earnings_transcripts",
        "source_name": "Earnings transcripts",
        "source_family": "transcript",
        "source_kind": "transcript_provider",
        "origin": "provider_candidate",
        "enabled": False,
        "ingestion_mode": "provider_required",
        "raw_access": "rights_required",
        "source_url": "",
        "notes": "Provider contract is intentionally unresolved; store permitted artifact refs only.",
    },
    {
        "source_id": "arco_birdclaw",
        "source_name": "Arco / Birdclaw",
        "source_family": "private_graph",
        "source_kind": "arco_bridge",
        "origin": "market",
        "enabled": True,
        "ingestion_mode": "arco_birdclaw_bridge",
        "raw_access": "local_private",
        "source_url": "/Users/joehu/brain/raw/sources/arco",
        "notes": "Market consumes summarized Arco/Birdclaw exports and source refs; it does not scrape X/Twitter.",
    },
    {
        "source_id": "odd_lots",
        "source_name": "Odd Lots",
        "source_family": "podcast",
        "source_kind": "rss",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "rss_or_feed",
        "raw_access": "public_feed",
        "source_url": "https://www.bloomberg.com/oddlots",
        "notes": "Podcast source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "stratechery",
        "source_name": "Stratechery",
        "source_family": "blog",
        "source_kind": "newsletter",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "permitted_feed_or_refs",
        "raw_access": "mixed_rights",
        "source_url": "https://stratechery.com/",
        "notes": "Store only permitted summaries/refs when content is paywalled.",
    },
    {
        "source_id": "a16z",
        "source_name": "a16z",
        "source_family": "podcast",
        "source_kind": "rss",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "rss_or_feed",
        "raw_access": "public_feed",
        "source_url": "https://a16z.com/podcasts/",
        "notes": "Podcast source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "ark_invest",
        "source_name": "ARK Invest",
        "source_family": "podcast",
        "source_kind": "rss_or_video",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "rss_youtube_or_feed",
        "raw_access": "public_feed",
        "source_url": "https://www.ark-invest.com/",
        "notes": "Podcast/video source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "peter_diamandis",
        "source_name": "Peter Diamandis",
        "source_family": "podcast",
        "source_kind": "rss_or_video",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "rss_youtube_or_feed",
        "raw_access": "public_feed",
        "source_url": "https://www.diamandis.com/",
        "notes": "Source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "sequoia_capital",
        "source_name": "Sequoia Capital",
        "source_family": "podcast",
        "source_kind": "rss_or_blog",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "rss_or_feed",
        "raw_access": "public_feed",
        "source_url": "https://www.sequoiacap.com/",
        "notes": "Source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "all_in",
        "source_name": "All-In",
        "source_family": "podcast",
        "source_kind": "rss_or_video",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "rss_youtube_or_feed",
        "raw_access": "public_feed",
        "source_url": "https://www.allinpodcast.co/",
        "notes": "Podcast source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "not_boring",
        "source_name": "Not Boring",
        "source_family": "blog",
        "source_kind": "newsletter",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "permitted_feed_or_refs",
        "raw_access": "mixed_rights",
        "source_url": "https://www.notboring.co/",
        "notes": "Store only permitted summaries/refs when content is paywalled.",
    },
    {
        "source_id": "in_good_company",
        "source_name": "In Good Company",
        "source_family": "podcast",
        "source_kind": "rss",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "rss_or_feed",
        "raw_access": "public_feed",
        "source_url": "",
        "notes": "Podcast source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "invest_like_the_best",
        "source_name": "Invest Like the Best",
        "source_family": "podcast",
        "source_kind": "rss",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "rss_or_feed",
        "raw_access": "public_feed",
        "source_url": "https://joincolossus.com/episodes",
        "notes": "Podcast source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "semianalysis",
        "source_name": "SemiAnalysis",
        "source_family": "blog",
        "source_kind": "newsletter",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "permitted_feed_or_refs",
        "raw_access": "mixed_rights",
        "source_url": "https://www.semianalysis.com/",
        "notes": "Store only permitted summaries/refs when content is paywalled.",
    },
    {
        "source_id": "no_priors",
        "source_name": "No Priors",
        "source_family": "podcast",
        "source_kind": "rss",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "rss_or_feed",
        "raw_access": "public_feed",
        "source_url": "https://www.nopriors.com/",
        "notes": "Podcast source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "dwarkesh_patel",
        "source_name": "Dwarkesh Patel",
        "source_family": "podcast",
        "source_kind": "rss_or_blog",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "rss_or_feed",
        "raw_access": "public_feed",
        "source_url": "https://www.dwarkeshpatel.com/",
        "notes": "Source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "cheeky_pint",
        "source_name": "Cheeky Pint",
        "source_family": "podcast",
        "source_kind": "rss_or_video",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "rss_youtube_or_feed",
        "raw_access": "public_feed",
        "source_url": "",
        "notes": "Source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "citrini_research",
        "source_name": "Citrini Research",
        "source_family": "blog",
        "source_kind": "newsletter",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "permitted_feed_or_refs",
        "raw_access": "mixed_rights",
        "source_url": "https://www.citriniresearch.com/",
        "notes": "Store only permitted summaries/refs when content is paywalled.",
    },
    {
        "source_id": "capital_wars",
        "source_name": "Capital Wars",
        "source_family": "blog",
        "source_kind": "newsletter",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "permitted_feed_or_refs",
        "raw_access": "mixed_rights",
        "source_url": "",
        "notes": "Source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "chamath_palihapitiya",
        "source_name": "Chamath Palihapitiya",
        "source_family": "blog",
        "source_kind": "blog_or_social_bridge",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "permitted_feed_or_arco_bridge",
        "raw_access": "mixed_rights",
        "source_url": "",
        "notes": "Use public/permitted refs or Arco/Birdclaw summaries; do not scrape social content in Market.",
    },
    {
        "source_id": "michael_burry",
        "source_name": "Michael Burry",
        "source_family": "blog",
        "source_kind": "filing_or_social_bridge",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "filings_or_arco_bridge",
        "raw_access": "mixed_rights",
        "source_url": "",
        "notes": "Use filings and permitted summaries/refs; do not scrape social content in Market.",
    },
    {
        "source_id": "naval",
        "source_name": "Naval",
        "source_family": "podcast",
        "source_kind": "rss_or_social_bridge",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "rss_or_arco_bridge",
        "raw_access": "mixed_rights",
        "source_url": "https://nav.al/",
        "notes": "Use feeds or Arco/Birdclaw summaries; do not scrape X/Twitter in Market.",
    },
    {
        "source_id": "avc",
        "source_name": "AVC",
        "source_family": "blog",
        "source_kind": "blog",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "rss_or_feed",
        "raw_access": "public_feed",
        "source_url": "https://avc.com/",
        "notes": "Blog source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "lex_fridman",
        "source_name": "Lex Fridman",
        "source_family": "podcast",
        "source_kind": "rss_or_video",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "rss_youtube_or_feed",
        "raw_access": "public_feed",
        "source_url": "https://lexfridman.com/podcast/",
        "notes": "Podcast source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "acquired",
        "source_name": "Acquired",
        "source_family": "podcast",
        "source_kind": "rss",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "rss_or_feed",
        "raw_access": "public_feed",
        "source_url": "https://www.acquired.fm/",
        "notes": "Podcast source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "benedict_evans",
        "source_name": "Benedict Evans",
        "source_family": "blog",
        "source_kind": "newsletter",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "permitted_feed_or_refs",
        "raw_access": "mixed_rights",
        "source_url": "https://www.ben-evans.com/",
        "notes": "Store only permitted summaries/refs when content is paywalled.",
    },
    {
        "source_id": "howard_marks",
        "source_name": "Howard Marks",
        "source_family": "blog",
        "source_kind": "memo",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "permitted_feed_or_refs",
        "raw_access": "mixed_rights",
        "source_url": "https://www.oaktreecapital.com/insights",
        "notes": "Memo source candidate from the MungerMode parity benchmark.",
    },
    {
        "source_id": "x_arco_watch_accounts",
        "source_name": "X accounts via Arco/Birdclaw",
        "source_family": "private_graph",
        "source_kind": "social_bridge",
        "origin": "mungermode_benchmark",
        "enabled": True,
        "ingestion_mode": "arco_birdclaw_bridge",
        "raw_access": "local_private_refs",
        "source_url": "/Users/joehu/brain/raw/sources/arco",
        "notes": "Covers @balajis, @karpathy, @citrini, @BillAckman, and @dylan522p through Arco/Birdclaw exports only.",
    },
]

SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,14}$")


def record_verified_sources(con: Any) -> None:
    ensure_source_registry(con)
    for source in VERIFIED_SOURCES:
        con.execute(
            """
            INSERT OR REPLACE INTO source_health
            (source, checked_at, status, detail, source_url)
            VALUES (?, ?, ?, ?, ?)
            """,
            [source["source"], datetime.utcnow().isoformat(), "verified_docs", source["detail"], source["source_url"]],
        )
    sync_canonical_sources(con)


def lightweight_online_check(con: Any, user_agent: str) -> None:
    ensure_source_registry(con)
    checks = [
        ("sec_edgar", "https://data.sec.gov/submissions/CIK0000320193.json"),
        ("coingecko", "https://api.coingecko.com/api/v3/ping"),
        ("defillama", "https://api.llama.fi/protocols"),
    ]
    with httpx.Client(timeout=8.0, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}) as client:
        for source, url in checks:
            status = "unreachable"
            detail = ""
            try:
                response = client.get(url)
                status = "ok" if response.status_code < 400 else f"http_{response.status_code}"
                detail = f"HTTP {response.status_code}"
            except Exception as exc:
                detail = str(exc)
            con.execute(
                """
                INSERT OR REPLACE INTO source_health
                (source, checked_at, status, detail, source_url)
                VALUES (?, ?, ?, ?, ?)
                """,
                [source, datetime.utcnow().isoformat(), status, detail, url],
            )
    sync_canonical_sources(con)


def ensure_source_registry(con: Any) -> None:
    now = datetime.now(UTC)
    for source in SOURCE_DEFINITIONS:
        con.execute(
            """
            INSERT OR REPLACE INTO source_registry
            (source_id, source_name, source_family, source_kind, origin, enabled, ingestion_mode,
             raw_access, source_url, notes, config, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, coalesce((SELECT created_at FROM source_registry WHERE source_id = ?), ?), ?)
            """,
            [
                source["source_id"],
                source["source_name"],
                source["source_family"],
                source["source_kind"],
                source["origin"],
                source["enabled"],
                source["ingestion_mode"],
                source["raw_access"],
                source.get("source_url") or "",
                source["notes"],
                json_dumps(source.get("config") or {}),
                source["source_id"],
                now,
                now,
            ],
        )
    for source_id, source_name, source_family, source_kind in _dynamic_sources(con):
        con.execute(
            """
            INSERT OR IGNORE INTO source_registry
            (source_id, source_name, source_family, source_kind, origin, enabled, ingestion_mode,
             raw_access, source_url, notes, config, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'market', true, 'existing_table_sync', 'local_row', '', 'Discovered from existing Market rows.', '{}', ?, ?)
            """,
            [source_id, source_name, source_family, source_kind, now, now],
        )


def sync_canonical_sources(con: Any) -> dict[str, Any]:
    ensure_source_registry(con)
    runs = 0
    items = 0
    signals = 0

    for row in query_rows(con, "SELECT id, provider, capability, started_at, finished_at, status, detail, raw FROM provider_runs"):
        source_id = slug(row.get("provider") or "provider")
        record_source_run(
            con,
            source_id=source_id,
            run_id=str(row.get("id") or stable_id("provider_run", row)),
            capability=str(row.get("capability") or "provider_run"),
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
            status=str(row.get("status") or "unknown"),
            failure_detail=str(row.get("detail") or ""),
            raw=parse_json(row.get("raw")),
        )
        runs += 1

    for row in query_rows(con, "SELECT source, checked_at, status, detail, source_url FROM source_health"):
        source_id = slug(row.get("source") or "source_health")
        record_source_run(
            con,
            source_id=source_id,
            run_id=stable_id("source_health", row.get("source"), row.get("checked_at")),
            capability="source_health",
            started_at=row.get("checked_at"),
            finished_at=row.get("checked_at"),
            status=str(row.get("status") or "unknown"),
            failure_detail=str(row.get("detail") or ""),
            raw=row,
        )
        runs += 1

    for row in query_rows(con, "SELECT id, published_at, provider, title, related_symbols, link, source, raw FROM news_items"):
        source_id = slug(row.get("provider") or row.get("source") or "news")
        symbols = symbols_from_value(row.get("related_symbols"))
        item_id = f"news:{row.get('id')}"
        upsert_source_item(
            con,
            {
                "id": item_id,
                "source_id": source_id,
                "source_kind": "news",
                "title": row.get("title"),
                "url": row.get("link"),
                "author": row.get("provider") or row.get("source"),
                "published_at": row.get("published_at"),
                "observed_at": row.get("published_at"),
                "summary": row.get("title"),
                "tickers": symbols,
                "evidence_refs": [row.get("link")] if row.get("link") else [],
                "raw": parse_json(row.get("raw")) or row,
                "license_status": "provider_link_only",
            },
        )
        items += 1
        signals += upsert_signals_for_item(con, item_id, source_id, symbols, row.get("published_at"), "news", row.get("title"), "", [row.get("link")] if row.get("link") else [])

    for row in query_rows(con, "SELECT id, symbol, author, created_at, thesis_summary, claims, source_url FROM birdclaw_theses"):
        symbol = normalize_signal_symbol(row.get("symbol"))
        if not symbol:
            continue
        source_id = "arco_birdclaw"
        item_id = f"arco_thesis:{row.get('id')}"
        claims = parse_json(row.get("claims"))
        refs = evidence_refs_from_claims(claims, row.get("source_url"))
        upsert_source_item(
            con,
            {
                "id": item_id,
                "source_id": source_id,
                "source_kind": "arco_thesis",
                "title": row.get("thesis_summary") or f"{symbol} thesis evidence",
                "url": row.get("source_url"),
                "author": row.get("author") or "Arco",
                "published_at": row.get("created_at"),
                "observed_at": row.get("created_at"),
                "summary": row.get("thesis_summary"),
                "tickers": [symbol],
                "evidence_refs": refs,
                "raw": claims,
                "license_status": "local_private_ref",
            },
        )
        items += 1
        signals += upsert_signals_for_item(con, item_id, source_id, [symbol], row.get("created_at"), "thesis", row.get("thesis_summary"), "", refs)

    for row in query_rows(con, "SELECT id, source_type, trader_name, filer_name, symbol, event_date, filed_date, action, amount, raw, source_url FROM disclosures"):
        source_type = str(row.get("source_type") or "disclosure")
        source_id = "sec_disclosures" if source_type in {"13f", "13f_holding", "form4"} else slug(source_type)
        raw = parse_json(row.get("raw"))
        symbols = [symbol for symbol in [normalize_signal_symbol(row.get("symbol"))] if symbol]
        for holding in raw.get("holdings", []) if isinstance(raw.get("holdings"), list) else []:
            symbol = normalize_signal_symbol(holding.get("symbol"))
            if symbol:
                symbols.append(symbol)
        symbols = sorted(set(symbols))
        if not symbols:
            continue
        title = f"{row.get('trader_name') or row.get('filer_name') or 'Disclosure'} {row.get('action') or source_type}".strip()
        item_id = f"disclosure:{row.get('id')}"
        upsert_source_item(
            con,
            {
                "id": item_id,
                "source_id": source_id,
                "source_kind": source_type,
                "title": title,
                "url": row.get("source_url"),
                "author": row.get("trader_name") or row.get("filer_name"),
                "published_at": row.get("filed_date") or row.get("event_date"),
                "observed_at": row.get("filed_date") or row.get("event_date"),
                "summary": title,
                "tickers": symbols,
                "evidence_refs": [row.get("source_url")] if row.get("source_url") else [],
                "raw": raw,
                "license_status": "public_filing",
            },
        )
        items += 1
        signals += upsert_signals_for_item(
            con,
            item_id,
            source_id,
            symbols,
            row.get("filed_date") or row.get("event_date"),
            "filing",
            title,
            "",
            [row.get("source_url")] if row.get("source_url") else [],
        )

    for row in query_rows(con, "SELECT run_id, symbol, observed_at, name, metrics, source FROM market_screener_rows"):
        symbol = normalize_signal_symbol(row.get("symbol"))
        if not symbol:
            continue
        source_id = slug(row.get("source") or "market_screener")
        item_id = stable_id("screener", row.get("run_id"), symbol, row.get("observed_at"))
        title = f"{row.get('name') or symbol} screener row"
        upsert_source_item(
            con,
            {
                "id": item_id,
                "source_id": source_id,
                "source_kind": "market_screener",
                "title": title,
                "url": "",
                "author": row.get("source") or "market_screener",
                "published_at": row.get("observed_at"),
                "observed_at": row.get("observed_at"),
                "summary": title,
                "tickers": [symbol],
                "evidence_refs": [],
                "raw": parse_json(row.get("metrics")),
                "license_status": "provider_metadata",
            },
        )
        items += 1
        signals += upsert_signals_for_item(con, item_id, source_id, [symbol], row.get("observed_at"), "screener", title, "", [])

    update_signal_market_context(con)
    runs += record_registry_run_placeholders(con)
    return {"status": "canonical_sources_synced", "runs": runs, "items": items, "signals": signals}


def ensure_canonical_sources(con: Any) -> dict[str, Any]:
    counts = query_rows(
        con,
        """
        SELECT
            (SELECT count(*) FROM source_registry) AS source_registry,
            (SELECT count(*) FROM source_runs) AS source_runs,
            (SELECT count(*) FROM source_items) AS source_items,
            (SELECT count(*) FROM ticker_source_signals) AS ticker_source_signals
        """,
    )[0]
    if any(int(counts.get(key) or 0) > 0 for key in counts):
        return {**counts, "status": "cached"}
    return sync_canonical_sources(con)


def record_source_run(
    con: Any,
    *,
    source_id: str,
    run_id: str,
    capability: str,
    started_at: Any,
    finished_at: Any,
    status: str,
    item_count: int = 0,
    ticker_count: int = 0,
    failure_detail: str = "",
    raw: dict[str, Any] | None = None,
) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO source_runs
        (source_id, run_id, capability, started_at, finished_at, status, item_count, ticker_count, failure_detail, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [source_id, run_id, capability, started_at, finished_at, status, item_count, ticker_count, failure_detail, json_dumps(raw or {})],
    )


def upsert_source_item(con: Any, item: dict[str, Any]) -> None:
    content_hash = item.get("content_hash") or stable_id(
        item.get("source_id"),
        item.get("title"),
        item.get("url"),
        item.get("published_at"),
        item.get("summary"),
    )
    con.execute(
        """
        INSERT OR REPLACE INTO source_items
        (id, source_id, source_run_id, source_kind, title, url, author, published_at, observed_at,
         summary, tickers, evidence_refs, raw, content_hash, license_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            item["id"],
            item["source_id"],
            item.get("source_run_id"),
            item.get("source_kind"),
            item.get("title"),
            item.get("url"),
            item.get("author"),
            item.get("published_at"),
            item.get("observed_at"),
            item.get("summary"),
            json_dumps(item.get("tickers") or []),
            json_dumps(item.get("evidence_refs") or []),
            json_dumps(item.get("raw") or {}),
            content_hash,
            item.get("license_status") or "unknown",
        ],
    )


def upsert_signals_for_item(
    con: Any,
    item_id: str,
    source_id: str,
    symbols: list[str],
    observed_at: Any,
    signal_type: str,
    thesis: Any,
    antithesis: Any,
    evidence_refs: list[Any],
) -> int:
    count = 0
    for symbol in sorted({normalize_signal_symbol(symbol) for symbol in symbols}):
        if not symbol:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO ticker_source_signals
            (id, source_item_id, source_id, symbol, observed_at, signal_type, sentiment, direction,
             confidence, thesis, antithesis, catalysts, risks, invalidation, evidence_refs, needs_market_context, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                stable_id("ticker_signal", item_id, source_id, symbol),
                item_id,
                source_id,
                symbol,
                observed_at,
                signal_type,
                infer_sentiment(thesis),
                "unknown",
                0.5,
                str(thesis or f"{symbol} appeared in {signal_type} source evidence."),
                str(antithesis or "No structured antithesis is loaded for this source item yet."),
                json_dumps([]),
                json_dumps([]),
                "",
                json_dumps([ref for ref in evidence_refs if ref]),
                True,
                json_dumps({"source_item_id": item_id}),
            ],
        )
        count += 1
    return count


def promote_source_signal_instruments(con: Any) -> int:
    rows = query_rows(
        con,
        """
        SELECT s.symbol, any_value(i.title) AS title, any_value(r.source_name) AS source_name, any_value(s.source_id) AS source_id
        FROM ticker_source_signals s
        LEFT JOIN source_items i ON i.id = s.source_item_id
        LEFT JOIN source_registry r ON r.source_id = s.source_id
        WHERE s.symbol IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM instruments existing WHERE upper(existing.symbol) = upper(s.symbol))
        GROUP BY s.symbol
        """,
    )
    for row in rows:
        symbol = normalize_signal_symbol(row.get("symbol"))
        if not symbol:
            continue
        upsert_instrument(
            con,
            {
                "symbol": symbol,
                "name": symbol,
                "asset_class": infer_asset_class(symbol),
                "category": "source-discovered",
                "source": f"source_signal:{row.get('source_id') or 'canonical'}",
            },
        )
    return len(rows)


def record_registry_run_placeholders(con: Any) -> int:
    now = datetime.now(UTC)
    count = 0
    rows = query_rows(
        con,
        """
        SELECT r.source_id,
               count(DISTINCT i.id) AS item_count,
               count(DISTINCT s.symbol) AS ticker_count
        FROM source_registry r
        LEFT JOIN source_items i ON i.source_id = r.source_id
        LEFT JOIN ticker_source_signals s ON s.source_id = r.source_id
        WHERE r.enabled = true
          AND NOT EXISTS (SELECT 1 FROM source_runs existing WHERE existing.source_id = r.source_id)
        GROUP BY r.source_id
        """,
    )
    for row in rows:
        item_count = int(row.get("item_count") or 0)
        ticker_count = int(row.get("ticker_count") or 0)
        record_source_run(
            con,
            source_id=str(row.get("source_id")),
            run_id=f"registry:{now.date().isoformat()}",
            capability="registry_config",
            started_at=now,
            finished_at=now,
            status="loaded" if item_count else "not_loaded",
            item_count=item_count,
            ticker_count=ticker_count,
            failure_detail="" if item_count else "No canonical source items are loaded yet.",
            raw={"source": "registry_placeholder"},
        )
        count += 1
    return count


def update_signal_market_context(con: Any) -> None:
    context_symbols = {
        normalize_signal_symbol(row.get("symbol"))
        for row in query_rows(
            con,
            """
            SELECT symbol FROM quotes_intraday
            UNION SELECT symbol FROM prices_daily
            UNION SELECT symbol FROM technical_features
            UNION SELECT symbol FROM liquidity_metrics
            UNION SELECT symbol FROM sepa_analyses
            UNION SELECT symbol FROM valuation_models
            """,
        )
    }
    for row in query_rows(con, "SELECT id, symbol FROM ticker_source_signals"):
        symbol = normalize_signal_symbol(row.get("symbol"))
        con.execute("UPDATE ticker_source_signals SET needs_market_context = ? WHERE id = ?", [symbol not in context_symbols, row.get("id")])


def source_registry_rows(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT
            r.*,
            latest.finished_at AS latest_run_at,
            latest.status AS latest_run_status,
            latest.failure_detail AS latest_failure_detail,
            coalesce(items.items_count, 0) AS items_count,
            coalesce(signals.tickers_count, 0) AS tickers_count,
            coalesce(signals.signals_count, 0) AS signals_count,
            coalesce(signals.needs_market_context_count, 0) AS needs_market_context_count
        FROM source_registry r
        LEFT JOIN (
            SELECT source_id, finished_at, status, failure_detail
            FROM source_runs
            QUALIFY row_number() OVER (PARTITION BY source_id ORDER BY finished_at DESC NULLS LAST, started_at DESC NULLS LAST) = 1
        ) latest ON latest.source_id = r.source_id
        LEFT JOIN (
            SELECT source_id, count(*) AS items_count FROM source_items GROUP BY source_id
        ) items ON items.source_id = r.source_id
        LEFT JOIN (
            SELECT source_id, count(*) AS signals_count, count(DISTINCT symbol) AS tickers_count,
                   sum(CASE WHEN needs_market_context THEN 1 ELSE 0 END) AS needs_market_context_count
            FROM ticker_source_signals
            GROUP BY source_id
        ) signals ON signals.source_id = r.source_id
        ORDER BY r.enabled DESC, items_count DESC, r.source_name
        """,
    )
    return [decode_row(row) | {"freshness": source_row_freshness(row)} for row in rows]


def source_item_rows(con: Any, source_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql = """
        SELECT i.*, r.source_name
        FROM source_items i
        LEFT JOIN source_registry r ON r.source_id = i.source_id
    """
    params: list[Any] = []
    if source_id:
        sql += " WHERE i.source_id = ?"
        params.append(source_id)
    sql += " ORDER BY i.observed_at DESC NULLS LAST, i.published_at DESC NULLS LAST LIMIT ?"
    params.append(limit)
    return [decode_row(row) for row in query_rows(con, sql, params)]


def source_run_rows(con: Any, source_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql = """
        SELECT runs.*, registry.source_name, registry.source_family
        FROM source_runs runs
        LEFT JOIN source_registry registry ON registry.source_id = runs.source_id
    """
    params: list[Any] = []
    if source_id:
        sql += " WHERE runs.source_id = ?"
        params.append(source_id)
    sql += " ORDER BY runs.finished_at DESC NULLS LAST, runs.started_at DESC NULLS LAST LIMIT ?"
    params.append(limit)
    return [decode_row(row) for row in query_rows(con, sql, params)]


def ticker_source_signal_rows(con: Any, symbol: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
    sql = """
        SELECT s.*, r.source_name, r.source_family, i.title, i.url
        FROM ticker_source_signals s
        LEFT JOIN source_registry r ON r.source_id = s.source_id
        LEFT JOIN source_items i ON i.id = s.source_item_id
    """
    params: list[Any] = []
    if symbol:
        sql += " WHERE upper(s.symbol) = ?"
        params.append(symbol.upper())
    sql += " ORDER BY s.observed_at DESC NULLS LAST LIMIT ?"
    params.append(limit)
    return [decode_row(row) for row in query_rows(con, sql, params)]


def source_detail_payload(con: Any, source_id: str) -> dict[str, Any]:
    ensure_canonical_sources(con)
    source = next((row for row in source_registry_rows(con) if row.get("source_id") == source_id), None)
    if not source:
        return {"source_id": source_id, "found": False, "items": [], "signals": []}
    return {
        "source": source,
        "runs": source_run_rows(con, source_id, limit=25),
        "items": source_item_rows(con, source_id, limit=100),
        "signals": [row for row in ticker_source_signal_rows(con, limit=500) if row.get("source_id") == source_id],
    }


def _dynamic_sources(con: Any) -> list[tuple[str, str, str, str]]:
    sources: dict[str, tuple[str, str, str, str]] = {}
    for row in query_rows(con, "SELECT DISTINCT provider FROM provider_runs WHERE provider IS NOT NULL"):
        provider = str(row.get("provider") or "")
        sources[slug(provider)] = (slug(provider), provider, "provider", "provider_run")
    for row in query_rows(con, "SELECT DISTINCT provider FROM news_items WHERE provider IS NOT NULL"):
        provider = str(row.get("provider") or "")
        sources[slug(provider)] = (slug(provider), provider, "news", "news")
    for row in query_rows(con, "SELECT DISTINCT source FROM market_screener_rows WHERE source IS NOT NULL"):
        provider = str(row.get("source") or "")
        sources[slug(provider)] = (slug(provider), provider, "market_data", "screener")
    return sorted(sources.values())


def symbols_from_value(value: Any) -> list[str]:
    parsed = parse_json(value)
    if isinstance(parsed, list):
        return sorted({symbol for item in parsed for symbol in [normalize_signal_symbol(item)] if symbol})
    if isinstance(parsed, str):
        return sorted({symbol for item in re.split(r"[,;\s]+", parsed) for symbol in [normalize_signal_symbol(item)] if symbol})
    return []


def normalize_signal_symbol(value: Any) -> str:
    symbol = normalize_symbol(str(value or ""))
    return symbol if symbol and SYMBOL_RE.match(symbol) else ""


def evidence_refs_from_claims(claims: Any, fallback_url: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(claims, dict):
        for item in claims.get("evidence", []) if isinstance(claims.get("evidence"), list) else []:
            if isinstance(item, dict):
                refs.extend(str(item.get(key)) for key in ("url", "source_url", "ref") if item.get(key))
            elif item:
                refs.append(str(item))
    if fallback_url:
        refs.append(str(fallback_url))
    return sorted(set(refs))


def source_row_freshness(row: dict[str, Any]) -> str:
    status = str(row.get("latest_run_status") or "").lower()
    if row.get("enabled") is False:
        return "disabled"
    if status in {"failed", "error"}:
        return "failed"
    if status in {"not_loaded", "configured"}:
        return "not_loaded"
    if row.get("items_count") or row.get("signals_count"):
        return "loaded"
    if row.get("latest_run_at"):
        return "checked"
    return "not_loaded"


def parse_json(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def decode_row(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for key in ("config", "tickers", "evidence_refs", "raw", "catalysts", "risks"):
        if key in decoded:
            decoded[key] = parse_json(decoded[key])
    return decoded


def infer_sentiment(value: Any) -> str:
    text = str(value or "").lower()
    if any(term in text for term in ("risk", "bear", "decline", "miss", "sell", "short", "weak")):
        return "bearish"
    if any(term in text for term in ("buy", "bull", "growth", "beat", "thesis", "strong", "upside")):
        return "bullish"
    return "neutral"


def stable_id(*parts: Any) -> str:
    joined = "|".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def slug(value: Any) -> str:
    text = str(value or "source").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "source"
