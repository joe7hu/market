"""Canonical source item and ticker-signal materialization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from investment_panel.core.db import json_dumps, query_rows, upsert_instrument
from investment_panel.core.instruments import infer_asset_class
from investment_panel.core.source_ingestion.registry import ensure_source_registry
from investment_panel.core.source_ingestion.utils import (
    evidence_refs_from_claims,
    infer_sentiment,
    normalize_signal_symbol,
    parse_json,
    slug,
    stable_id,
    symbols_from_value,
)

SEC_DISCLOSURE_TYPES = {
    "13f",
    "13f_holding",
    "form4",
    "public_disclosure_transaction",
    "trader_portfolio_model",
}

SEC_FILING_SOURCE_BY_FORM = {
    "8-K": {
        "source_id": "sec_material_events_8k",
        "source_name": "Material Events (8-K)",
    },
    "10-Q": {
        "source_id": "sec_quarterly_reports_10q",
        "source_name": "Quarterly Reports (10-Q)",
    },
    "10-K": {
        "source_id": "sec_annual_reports_10k",
        "source_name": "Annual Reports (10-K)",
    },
    "DEF 14A": {
        "source_id": "sec_proxy_statements_def14a",
        "source_name": "Proxy Statements (DEF 14A)",
    },
    "6-K": {
        "source_id": "sec_foreign_reports_6k",
        "source_name": "Foreign Reports (6-K)",
    },
}


@dataclass(frozen=True)
class SourceSyncCounts:
    runs: int = 0
    items: int = 0
    signals: int = 0

    def __add__(self, other: "SourceSyncCounts") -> "SourceSyncCounts":
        return SourceSyncCounts(
            runs=self.runs + other.runs,
            items=self.items + other.items,
            signals=self.signals + other.signals,
        )


SourceSyncAdapter = Callable[[Any], SourceSyncCounts]


SOURCE_SYNC_ADAPTERS: tuple[SourceSyncAdapter, ...] = (
    lambda con: _sync_provider_runs(con),
    lambda con: _sync_source_health_runs(con),
    lambda con: _sync_news_items(con),
    lambda con: _sync_arco_theses(con),
    lambda con: _sync_disclosures(con),
    lambda con: _sync_market_screener_rows(con),
    lambda con: _sync_equity_fundamentals(con),
    lambda con: _sync_crypto_fundamentals(con),
    lambda con: _sync_earnings_events(con),
    lambda con: _sync_analyst_estimates(con),
)


def sync_canonical_sources(con: Any) -> dict[str, Any]:
    """Sync every source family into canonical source items and ticker signals."""

    ensure_source_registry(con)
    counts = SourceSyncCounts()
    for adapter in SOURCE_SYNC_ADAPTERS:
        counts += adapter(con)
    update_signal_market_context(con)
    counts += SourceSyncCounts(runs=record_registry_run_placeholders(con))
    return {
        "status": "canonical_sources_synced",
        "runs": counts.runs,
        "items": counts.items,
        "signals": counts.signals,
    }


def _sync_provider_runs(con: Any) -> SourceSyncCounts:
    runs = 0

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
    return SourceSyncCounts(runs=runs)


def _sync_source_health_runs(con: Any) -> SourceSyncCounts:
    runs = 0
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
    return SourceSyncCounts(runs=runs)


def _sync_news_items(con: Any) -> SourceSyncCounts:
    items = 0
    signals = 0
    for row in query_rows(con, "SELECT id, published_at, provider, title, related_symbols, link, source, raw FROM news_items"):
        source_id = slug(row.get("provider") or row.get("source") or "news")
        symbols = symbols_from_value(row.get("related_symbols"))
        item_id = f"news:{row.get('id')}"
        stored_items, stored_signals = store_item_with_signals(
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
            signal_type="news",
            thesis=row.get("title"),
            evidence_refs=[row.get("link")] if row.get("link") else [],
        )
        items += stored_items
        signals += stored_signals
    return SourceSyncCounts(items=items, signals=signals)


def _sync_arco_theses(con: Any) -> SourceSyncCounts:
    items = 0
    signals = 0
    for row in query_rows(con, "SELECT id, symbol, author, created_at, thesis_summary, claims, source_url FROM birdclaw_theses"):
        symbol = normalize_signal_symbol(row.get("symbol"))
        if not symbol:
            continue
        source_id = "arco_birdclaw"
        item_id = f"arco_thesis:{row.get('id')}"
        claims = parse_json(row.get("claims"))
        refs = evidence_refs_from_claims(claims, row.get("source_url"))
        stored_items, stored_signals = store_item_with_signals(
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
            signal_type="thesis",
            thesis=row.get("thesis_summary"),
            evidence_refs=refs,
        )
        items += stored_items
        signals += stored_signals
    return SourceSyncCounts(items=items, signals=signals)


def _sync_disclosures(con: Any) -> SourceSyncCounts:
    items = 0
    signals = 0
    for row in query_rows(con, "SELECT id, source_type, trader_name, filer_name, symbol, event_date, filed_date, action, amount, raw, source_url FROM disclosures"):
        source_type = str(row.get("source_type") or "disclosure")
        source_id = "sec_disclosures" if source_type in SEC_DISCLOSURE_TYPES else slug(source_type)
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
        refs = [row.get("source_url")] if row.get("source_url") else []
        stored_items, stored_signals = store_item_with_signals(
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
                "evidence_refs": refs,
                "raw": raw,
                "license_status": "public_filing",
            },
            signal_type="filing",
            thesis=title,
            evidence_refs=refs,
        )
        items += stored_items
        signals += stored_signals
    return SourceSyncCounts(items=items, signals=signals)


def _sync_market_screener_rows(con: Any) -> SourceSyncCounts:
    items = 0
    signals = 0
    for row in query_rows(con, "SELECT run_id, symbol, observed_at, name, metrics, source FROM market_screener_rows"):
        symbol = normalize_signal_symbol(row.get("symbol"))
        if not symbol:
            continue
        source_id = slug(row.get("source") or "market_screener")
        item_id = stable_id("screener", row.get("run_id"), symbol, row.get("observed_at"))
        title = f"{row.get('name') or symbol} screener row"
        stored_items, stored_signals = store_item_with_signals(
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
            signal_type="screener",
            thesis=title,
            evidence_refs=[],
        )
        items += stored_items
        signals += stored_signals
    return SourceSyncCounts(items=items, signals=signals)


def _sync_equity_fundamentals(con: Any) -> SourceSyncCounts:
    items = 0
    signals = 0
    for row in query_rows(con, "SELECT symbol, period_end, filing_date, form_type, metrics, source_url FROM equity_fundamentals"):
        symbol = normalize_signal_symbol(row.get("symbol"))
        if not symbol:
            continue
        form_type = normalize_sec_form_type(row.get("form_type"))
        filing_source = sec_filing_source_for_form(form_type)
        period_end = row.get("period_end")
        title = f"{symbol} {form_type} fundamentals"
        if period_end:
            title = f"{title} for {period_end}"
        refs = [row.get("source_url")] if row.get("source_url") else []
        if filing_source["source_id"] != "sec_edgar":
            legacy_item_id = stable_id("equity_fundamental", symbol, period_end, form_type)
            con.execute("DELETE FROM ticker_source_signals WHERE source_item_id = ?", [legacy_item_id])
            con.execute("DELETE FROM source_items WHERE id = ?", [legacy_item_id])
        stored_items, stored_signals = store_item_with_signals(
            con,
            {
                "id": stable_id("sec_filing", filing_source["source_id"], symbol, period_end, form_type),
                "source_id": filing_source["source_id"],
                "source_kind": "equity_fundamental",
                "title": title,
                "url": row.get("source_url"),
                "author": filing_source["source_name"],
                "published_at": row.get("filing_date") or period_end,
                "observed_at": row.get("filing_date") or period_end,
                "summary": title,
                "tickers": [symbol],
                "evidence_refs": refs,
                "raw": parse_json(row.get("metrics")),
                "license_status": "public_filing",
            },
            signal_type="fundamental",
            thesis=title,
            evidence_refs=refs,
        )
        items += stored_items
        signals += stored_signals
    return SourceSyncCounts(items=items, signals=signals)


def _sync_crypto_fundamentals(con: Any) -> SourceSyncCounts:
    items = 0
    signals = 0
    for row in query_rows(con, "SELECT symbol, date, metrics, source FROM crypto_fundamentals"):
        symbol = normalize_signal_symbol(row.get("symbol"))
        if not symbol:
            continue
        source_id = slug(row.get("source") or "coingecko")
        title = f"{symbol} crypto fundamentals"
        if row.get("date"):
            title = f"{title} for {row.get('date')}"
        stored_items, stored_signals = store_item_with_signals(
            con,
            {
                "id": stable_id("crypto_fundamental", source_id, symbol, row.get("date")),
                "source_id": source_id,
                "source_kind": "crypto_fundamental",
                "title": title,
                "url": "",
                "author": row.get("source") or "crypto_fundamentals",
                "published_at": row.get("date"),
                "observed_at": row.get("date"),
                "summary": title,
                "tickers": [symbol],
                "evidence_refs": [],
                "raw": parse_json(row.get("metrics")),
                "license_status": "provider_metadata",
            },
            signal_type="fundamental",
            thesis=title,
            evidence_refs=[],
        )
        items += stored_items
        signals += stored_signals
    return SourceSyncCounts(items=items, signals=signals)


def _sync_earnings_events(con: Any) -> SourceSyncCounts:
    items = 0
    signals = 0
    for row in query_rows(con, "SELECT symbol, event_date, event_type, metrics, source FROM earnings_events"):
        symbol = normalize_signal_symbol(row.get("symbol"))
        if not symbol:
            continue
        source_id = slug(row.get("source") or "earnings")
        event_type = str(row.get("event_type") or "earnings")
        title = f"{symbol} {event_type} event"
        if row.get("event_date"):
            title = f"{title} on {row.get('event_date')}"
        stored_items, stored_signals = store_item_with_signals(
            con,
            {
                "id": stable_id("earnings_event", source_id, symbol, row.get("event_date"), event_type),
                "source_id": source_id,
                "source_kind": "earnings_event",
                "title": title,
                "url": "",
                "author": row.get("source") or "earnings_events",
                "published_at": row.get("event_date"),
                "observed_at": row.get("event_date"),
                "summary": title,
                "tickers": [symbol],
                "evidence_refs": [],
                "raw": parse_json(row.get("metrics")),
                "license_status": "provider_metadata",
            },
            signal_type="earnings_event",
            thesis=title,
            evidence_refs=[],
        )
        items += stored_items
        signals += stored_signals
    return SourceSyncCounts(items=items, signals=signals)


def _sync_analyst_estimates(con: Any) -> SourceSyncCounts:
    items = 0
    signals = 0
    for row in query_rows(con, "SELECT symbol, as_of, estimates, source FROM analyst_estimates"):
        symbol = normalize_signal_symbol(row.get("symbol"))
        if not symbol:
            continue
        source_id = slug(row.get("source") or "analyst_estimates")
        title = f"{symbol} analyst estimates"
        if row.get("as_of"):
            title = f"{title} as of {row.get('as_of')}"
        stored_items, stored_signals = store_item_with_signals(
            con,
            {
                "id": stable_id("analyst_estimate", source_id, symbol, row.get("as_of")),
                "source_id": source_id,
                "source_kind": "analyst_estimate",
                "title": title,
                "url": "",
                "author": row.get("source") or "analyst_estimates",
                "published_at": row.get("as_of"),
                "observed_at": row.get("as_of"),
                "summary": title,
                "tickers": [symbol],
                "evidence_refs": [],
                "raw": parse_json(row.get("estimates")),
                "license_status": "provider_metadata",
            },
            signal_type="analyst_estimate",
            thesis=title,
            evidence_refs=[],
        )
        items += stored_items
        signals += stored_signals
    return SourceSyncCounts(items=items, signals=signals)


def ensure_canonical_sources(con: Any) -> dict[str, Any]:
    ensure_source_registry(con)
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
    if any(int(counts.get(key) or 0) > 0 for key in counts) and not canonical_sources_need_sync(con):
        return {**counts, "status": "cached"}
    return sync_canonical_sources(con)


def canonical_sources_need_sync(con: Any) -> bool:
    """Detect source registry/canonical drift from newly mapped source families."""

    if missing_registry_definitions(con):
        return True
    return bool(sec_filing_rows_missing_canonical_sources(con))


def missing_registry_definitions(con: Any) -> list[str]:
    rows = query_rows(
        con,
        """
        SELECT source_id
        FROM source_registry
        WHERE source_id IN (
            'sec_material_events_8k',
            'sec_quarterly_reports_10q',
            'sec_annual_reports_10k',
            'sec_proxy_statements_def14a',
            'sec_foreign_reports_6k'
        )
        """,
    )
    present = {str(row.get("source_id")) for row in rows}
    return sorted({source["source_id"] for source in SEC_FILING_SOURCE_BY_FORM.values()} - present)


def sec_filing_rows_missing_canonical_sources(con: Any) -> list[str]:
    rows = query_rows(
        con,
        """
        SELECT symbol, period_end, form_type
        FROM equity_fundamentals
        WHERE upper(form_type) IN ('8-K', '10-Q', '10-K', 'DEF 14A', '6-K')
        """,
    )
    missing: set[str] = set()
    for row in rows:
        form_type = normalize_sec_form_type(row.get("form_type"))
        source = sec_filing_source_for_form(form_type)
        expected_item_id = stable_id("sec_filing", source["source_id"], normalize_signal_symbol(row.get("symbol")), row.get("period_end"), form_type)
        existing = query_rows(con, "SELECT 1 FROM source_items WHERE id = ? LIMIT 1", [expected_item_id])
        if not existing:
            missing.add(source["source_id"])
    return sorted(missing)


def normalize_sec_form_type(value: Any) -> str:
    return str(value or "filing").strip().upper()


def sec_filing_source_for_form(form_type: str) -> dict[str, str]:
    return SEC_FILING_SOURCE_BY_FORM.get(normalize_sec_form_type(form_type), {"source_id": "sec_edgar", "source_name": "SEC EDGAR"})


def store_item_with_signals(
    con: Any,
    item: dict[str, Any],
    *,
    signal_type: str,
    thesis: Any,
    evidence_refs: list[Any],
    antithesis: Any = "",
) -> tuple[int, int]:
    upsert_source_item(con, item)
    con.execute("DELETE FROM ticker_source_signals WHERE source_item_id = ?", [item["id"]])
    symbols = [symbol for symbol in item.get("tickers", []) if symbol]
    signals = upsert_signals_for_item(
        con,
        str(item["id"]),
        str(item["source_id"]),
        symbols,
        item.get("observed_at") or item.get("published_at"),
        signal_type,
        thesis,
        antithesis,
        evidence_refs,
    )
    return 1, signals


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
    evidence_refs = _source_item_evidence_refs(item)
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
            json_dumps(evidence_refs),
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
    refs = _signal_evidence_refs(item_id, evidence_refs)
    catalysts = _signal_catalysts(signal_type, thesis)
    risks = _signal_risks(signal_type, source_id)
    invalidation = _signal_invalidation(signal_type)
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
                json_dumps(catalysts),
                json_dumps(risks),
                invalidation,
                json_dumps(refs),
                True,
                json_dumps({"source_item_id": item_id}),
            ],
        )
        count += 1
    return count


def _source_item_evidence_refs(item: dict[str, Any]) -> list[str]:
    refs = [str(ref).strip() for ref in item.get("evidence_refs") or [] if str(ref).strip()]
    url = str(item.get("url") or "").strip()
    if url:
        refs.append(url)
    if not refs:
        refs.append(f"source_item:{item['id']}")
    return list(dict.fromkeys(refs))


def _signal_evidence_refs(item_id: str, evidence_refs: list[Any]) -> list[str]:
    refs = [str(ref).strip() for ref in evidence_refs if str(ref).strip()]
    return refs or [f"source_item:{item_id}"]


def _signal_catalysts(signal_type: str, thesis: Any) -> list[str]:
    text = str(thesis or "").strip()
    if text:
        return [text]
    return [f"Review {signal_type.replace('_', ' ')} evidence for catalyst context."]


def _signal_risks(signal_type: str, source_id: str) -> list[str]:
    return [f"Validate {signal_type.replace('_', ' ')} signal from {source_id} against current market context before acting."]


def _signal_invalidation(signal_type: str) -> str:
    return f"No structured invalidation loaded for this {signal_type.replace('_', ' ')} signal; require ticker thesis review before action."


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
