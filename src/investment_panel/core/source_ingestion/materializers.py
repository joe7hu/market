"""Source-family row adapters for canonical source materialization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from investment_panel.core.db import query_rows
from investment_panel.core.source_ingestion.utils import (
    evidence_refs_from_claims,
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
    "8-K": {"source_id": "sec_material_events_8k", "source_name": "Material Events (8-K)"},
    "10-Q": {"source_id": "sec_quarterly_reports_10q", "source_name": "Quarterly Reports (10-Q)"},
    "10-K": {"source_id": "sec_annual_reports_10k", "source_name": "Annual Reports (10-K)"},
    "DEF 14A": {"source_id": "sec_proxy_statements_def14a", "source_name": "Proxy Statements (DEF 14A)"},
    "6-K": {"source_id": "sec_foreign_reports_6k", "source_name": "Foreign Reports (6-K)"},
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


@dataclass(frozen=True)
class SourceMaterialization:
    item: dict[str, Any]
    signal_type: str
    thesis: Any
    evidence_refs: list[Any]
    antithesis: Any = ""


SourceItemBuilder = Callable[[Any, dict[str, Any]], SourceMaterialization | None]
SourceItemStore = Callable[..., tuple[int, int]]


def sync_source_items(
    con: Any,
    sql: str,
    builder: SourceItemBuilder,
    store_item_with_signals: SourceItemStore,
) -> SourceSyncCounts:
    items = 0
    signals = 0
    for row in query_rows(con, sql):
        materialized = builder(con, row)
        if materialized is None:
            continue
        stored_items, stored_signals = store_item_with_signals(
            con,
            materialized.item,
            signal_type=materialized.signal_type,
            thesis=materialized.thesis,
            evidence_refs=materialized.evidence_refs,
            antithesis=materialized.antithesis,
        )
        items += stored_items
        signals += stored_signals
    return SourceSyncCounts(items=items, signals=signals)


def news_item(_con: Any, row: dict[str, Any]) -> SourceMaterialization:
    source_id = slug(row.get("provider") or row.get("source") or "news")
    refs = [row.get("link")] if row.get("link") else []
    return SourceMaterialization(
        item={
            "id": f"news:{row.get('id')}",
            "source_id": source_id,
            "source_kind": "news",
            "title": row.get("title"),
            "url": row.get("link"),
            "author": row.get("provider") or row.get("source"),
            "published_at": row.get("published_at"),
            "observed_at": row.get("published_at"),
            "summary": row.get("title"),
            "tickers": symbols_from_value(row.get("related_symbols")),
            "evidence_refs": refs,
            "raw": parse_json(row.get("raw")) or row,
            "license_status": "provider_link_only",
        },
        signal_type="news",
        thesis=row.get("title"),
        evidence_refs=refs,
    )


def arco_thesis_item(_con: Any, row: dict[str, Any]) -> SourceMaterialization | None:
    symbol = normalize_signal_symbol(row.get("symbol"))
    if not symbol:
        return None
    claims = parse_json(row.get("claims"))
    refs = evidence_refs_from_claims(claims, row.get("source_url"))
    return SourceMaterialization(
        item={
            "id": f"arco_thesis:{row.get('id')}",
            "source_id": "arco_birdclaw",
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


def disclosure_item(_con: Any, row: dict[str, Any]) -> SourceMaterialization | None:
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
        return None
    title = f"{row.get('trader_name') or row.get('filer_name') or 'Disclosure'} {row.get('action') or source_type}".strip()
    refs = [row.get("source_url")] if row.get("source_url") else []
    return SourceMaterialization(
        item={
            "id": f"disclosure:{row.get('id')}",
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


def market_screener_item(_con: Any, row: dict[str, Any]) -> SourceMaterialization | None:
    symbol = normalize_signal_symbol(row.get("symbol"))
    if not symbol:
        return None
    source_id = slug(row.get("source") or "market_screener")
    title = f"{row.get('name') or symbol} screener row"
    return SourceMaterialization(
        item={
            "id": stable_id("screener", row.get("run_id"), symbol, row.get("observed_at")),
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


def equity_fundamental_item(con: Any, row: dict[str, Any]) -> SourceMaterialization | None:
    symbol = normalize_signal_symbol(row.get("symbol"))
    if not symbol:
        return None
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
    return SourceMaterialization(
        item={
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


def crypto_fundamental_item(_con: Any, row: dict[str, Any]) -> SourceMaterialization | None:
    symbol = normalize_signal_symbol(row.get("symbol"))
    if not symbol:
        return None
    source_id = slug(row.get("source") or "coingecko")
    title = f"{symbol} crypto fundamentals"
    if row.get("date"):
        title = f"{title} for {row.get('date')}"
    return SourceMaterialization(
        item={
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


def earnings_event_item(_con: Any, row: dict[str, Any]) -> SourceMaterialization | None:
    symbol = normalize_signal_symbol(row.get("symbol"))
    if not symbol:
        return None
    source_id = slug(row.get("source") or "earnings")
    event_type = str(row.get("event_type") or "earnings")
    title = f"{symbol} {event_type} event"
    if row.get("event_date"):
        title = f"{title} on {row.get('event_date')}"
    return SourceMaterialization(
        item={
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


def analyst_estimate_item(_con: Any, row: dict[str, Any]) -> SourceMaterialization | None:
    symbol = normalize_signal_symbol(row.get("symbol"))
    if not symbol:
        return None
    source_id = slug(row.get("source") or "analyst_estimates")
    title = f"{symbol} analyst estimates"
    if row.get("as_of"):
        title = f"{title} as of {row.get('as_of')}"
    return SourceMaterialization(
        item={
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


def normalize_sec_form_type(value: Any) -> str:
    return str(value or "filing").strip().upper()


def sec_filing_source_for_form(form_type: str) -> dict[str, str]:
    return SEC_FILING_SOURCE_BY_FORM.get(normalize_sec_form_type(form_type), {"source_id": "sec_edgar", "source_name": "SEC EDGAR"})
