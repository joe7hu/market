"""Source provenance and family-row helpers."""

from __future__ import annotations
from typing import Any
from investment_panel.core.db import db, init_db, query_rows

from investment_panel.core.panel.coerce import _dict_from_value, _normalize_symbol_token, _plain_text, _string_list, _symbols_from_value
from investment_panel.core.panel.disclosures import _allocation_key, _compact_empty_fields, disclosures



def _expanded_disclosure_positions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for row in rows:
        if row.get("source_type") != "13f":
            expanded.append(row)
            continue
        holding_sample = row.get("holding_sample") if isinstance(row.get("holding_sample"), list) else []
        allocation_by_key = {
            _allocation_key(item): item
            for item in row.get("allocation_history", [])
            if isinstance(item, dict)
        }
        for holding in holding_sample:
            if not isinstance(holding, dict):
                continue
            symbol = _normalize_symbol_token(holding.get("symbol"))
            if not symbol:
                continue
            allocation = allocation_by_key.get(_allocation_key(holding), {})
            action = str(allocation.get("type") or "HOLDINGS")
            value = float(holding.get("market_value") or holding.get("value_thousands") or 0.0)
            expanded.append(
                {
                    **row,
                    "symbol": symbol,
                    "action": action,
                    "amount": value,
                    "total_value": value,
                    "holding_weight": holding.get("weight"),
                    "holding_security": holding.get("name"),
                    "holding_put_call": holding.get("put_call"),
                    "raw": {
                        "holding": holding,
                        "allocation": allocation,
                        "source_type": "13f_holding",
                        "value_thousands": value,
                    },
                }
            )
    return expanded




def _countercase(symbol: str, decision: dict[str, Any], thesis: dict[str, Any], row: dict[str, Any]) -> str:
    for value in (
        thesis.get("invalidation"),
        decision.get("invalidation"),
        row.get("blocker"),
    ):
        text = _plain_text(value)
        if text and text.lower() != "none":
            return text
    freshness = str(decision.get("freshness_status") or "")
    if freshness and freshness.lower() not in {"fresh", "current"}:
        return f"The {symbol} signal weakens if source freshness remains {freshness} or primary evidence is not refreshed."
    return "The countercase is that this signal is already reflected in price or lacks enough independent source confirmation."




def _source_family_counts(decision_rows: list[dict[str, Any]]) -> dict[str, tuple[list[str], list[str]]]:
    output: dict[str, tuple[list[str], list[str]]] = {}
    for row in decision_rows:
        symbol = str(row.get("symbol") or "").upper()
        grade = str(row.get("action_grade") or "").lower()
        basis = _dict_from_value(row.get("decision_basis"))
        counts = basis.get("source_counts") if isinstance(basis.get("source_counts"), dict) else _dict_from_value(row.get("source_counts"))
        for key, value in counts.items() if isinstance(counts, dict) else []:
            if not value:
                continue
            for family_key in {str(key), _source_family_for_name(str(key).lower())}:
                bullish, bearish = output.setdefault(family_key, ([], []))
                target = bearish if "reject" in grade or "stale" in grade else bullish
                if symbol and symbol not in target:
                    target.append(symbol)
    return output




def _source_count_rows(con: Any, source_name: str, content_type: str, table_name: str, symbol_column: str, time_column: str) -> list[dict[str, Any]]:
    try:
        result = query_rows(
            con,
            f"""
            SELECT count(*) AS items_count,
                   count(DISTINCT {symbol_column}) AS tickers_count,
                   max({time_column}) AS latest_at
            FROM {table_name}
            """,
        )[0]
    except Exception:
        return []
    count = int(result.get("items_count") or 0)
    if count <= 0:
        return []
    return [
        {
            "source_name": source_name,
            "content_type": content_type,
            "items_count": count,
            "tickers_count": int(result.get("tickers_count") or 0),
            "latest_at": result.get("latest_at"),
        }
    ]




def _news_provider_source_rows(con: Any) -> list[dict[str, Any]]:
    provider_rows: dict[str, dict[str, Any]] = {}
    for row in query_rows(
        con,
        """
        SELECT provider, title, related_symbols, published_at, link
        FROM news_items
        WHERE provider IS NOT NULL
        ORDER BY published_at DESC
        LIMIT 600
        """,
    ):
        provider = str(row.get("provider") or "News")
        entry = provider_rows.setdefault(
            provider,
            {
                "source_name": provider,
                "content_type": "news",
                "items_count": 0,
                "tickers": set(),
                "latest_at": row.get("published_at"),
                "bullish": {},
                "bearish": {},
                "history": [],
            },
        )
        entry["items_count"] += 1
        entry["latest_at"] = max(str(entry.get("latest_at") or ""), str(row.get("published_at") or ""))
        symbols = _symbols_from_value(row.get("related_symbols"))
        sentiment = _source_sentiment(str(row.get("title") or ""), "")
        for symbol in symbols:
            entry["tickers"].add(symbol)
            if sentiment == "bearish":
                entry["bearish"][symbol] = int(entry["bearish"].get(symbol, 0)) + 1
            elif sentiment == "bullish":
                entry["bullish"][symbol] = int(entry["bullish"].get(symbol, 0)) + 1
        if symbols:
            entry["history"].append({"date": row.get("published_at"), "symbols": symbols[:4], "title": row.get("title"), "url": row.get("link"), "sentiment": sentiment})
    return [_source_row_from_entry(entry, "news_provider") for entry in provider_rows.values()]




def _thesis_author_source_rows(con: Any) -> list[dict[str, Any]]:
    author_rows: dict[str, dict[str, Any]] = {}
    for row in query_rows(
        con,
        """
        SELECT author, symbol, created_at, thesis_summary, source_url
        FROM birdclaw_theses
        ORDER BY created_at DESC
        LIMIT 300
        """,
    ):
        author = str(row.get("author") or "Arco / Birdclaw")
        symbol = _normalize_symbol_token(row.get("symbol"))
        entry = author_rows.setdefault(
            author,
            {"source_name": author, "content_type": "private_graph", "items_count": 0, "tickers": set(), "latest_at": row.get("created_at"), "bullish": {}, "bearish": {}, "history": []},
        )
        entry["items_count"] += 1
        entry["latest_at"] = max(str(entry.get("latest_at") or ""), str(row.get("created_at") or ""))
        if symbol:
            entry["tickers"].add(symbol)
            entry["bullish"][symbol] = int(entry["bullish"].get(symbol, 0)) + 1
            entry["history"].append({"date": row.get("created_at"), "symbols": [symbol], "title": row.get("thesis_summary"), "url": row.get("source_url"), "sentiment": "bullish"})
    return [_source_row_from_entry(entry, "private_source") for entry in author_rows.values()]




def _disclosure_investor_source_rows(con: Any) -> list[dict[str, Any]]:
    investor_rows: dict[str, dict[str, Any]] = {}
    for row in _expanded_disclosure_positions(disclosures(con)):
        symbol = _normalize_symbol_token(row.get("symbol"))
        investor = str(row.get("trader_name") or row.get("filer_name") or "Tracked investor")
        if not symbol:
            continue
        entry = investor_rows.setdefault(
            investor,
            {"source_name": investor, "content_type": "filing", "items_count": 0, "tickers": set(), "latest_at": row.get("filed_date"), "bullish": {}, "bearish": {}, "history": []},
        )
        action = str(row.get("action") or "")
        sentiment = _source_sentiment("", action)
        entry["items_count"] += 1
        entry["latest_at"] = max(str(entry.get("latest_at") or ""), str(row.get("filed_date") or row.get("event_date") or ""))
        entry["tickers"].add(symbol)
        target = "bearish" if sentiment == "bearish" else "bullish"
        entry[target][symbol] = int(entry[target].get(symbol, 0)) + 1
        entry["history"].append({"date": row.get("filed_date") or row.get("event_date"), "symbols": [symbol], "title": f"{action} {symbol}", "url": row.get("source_url"), "sentiment": sentiment})
    return [_source_row_from_entry(entry, "disclosure_source") for entry in investor_rows.values()]




def _research_report_source_rows(con: Any) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in query_rows(
        con,
        """
        SELECT symbol, created_at, report_type, report_json
        FROM research_reports
        ORDER BY created_at DESC
        LIMIT 200
        """,
    ):
        source_name = f"Market {str(row.get('report_type') or 'research').replace('_', ' ')}"
        symbol = _normalize_symbol_token(row.get("symbol"))
        if not symbol or symbol == "PORTFOLIO":
            continue
        report = _dict_from_value(row.get("report_json"))
        sentiment = "bearish" if _plain_text(report.get("decision")).lower() in {"avoid", "sell", "reject"} else "bullish"
        entry = rows.setdefault(
            source_name,
            {"source_name": source_name, "content_type": "research", "items_count": 0, "tickers": set(), "latest_at": row.get("created_at"), "bullish": {}, "bearish": {}, "history": []},
        )
        entry["items_count"] += 1
        entry["latest_at"] = max(str(entry.get("latest_at") or ""), str(row.get("created_at") or ""))
        entry["tickers"].add(symbol)
        entry["bearish" if sentiment == "bearish" else "bullish"][symbol] = int(entry["bearish" if sentiment == "bearish" else "bullish"].get(symbol, 0)) + 1
        entry["history"].append({"date": row.get("created_at"), "symbols": [symbol], "title": report.get("decision") or row.get("report_type"), "sentiment": sentiment})
    return [_source_row_from_entry(entry, "research_source") for entry in rows.values()]




def _source_row_from_entry(entry: dict[str, Any], origin: str) -> dict[str, Any]:
    bullish = _ranked_symbol_counts(entry.get("bullish", {}))
    bearish = _ranked_symbol_counts(entry.get("bearish", {}))
    return {
        "source_name": entry["source_name"],
        "content_type": entry["content_type"],
        "items_count": int(entry.get("items_count") or 0),
        "tickers_count": len(entry.get("tickers") or []),
        "latest_at": entry.get("latest_at"),
        "bullish_symbols": [symbol for symbol, _ in bullish[:12]],
        "bearish_symbols": [symbol for symbol, _ in bearish[:12]],
        "ticker_history": entry.get("history", [])[:12],
        "source_origin": origin,
    }




def _ranked_symbol_counts(counts: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(((symbol, int(count or 0)) for symbol, count in counts.items()), key=lambda item: (item[1], item[0]), reverse=True)




def _source_sentiment(title: str, action: str) -> str:
    text = f"{title} {action}".lower()
    bearish_terms = ("sell", "sold", "sale", "reduce", "reduced", "cut", "downgrade", "miss", "falls", "fell", "drops", "plunge", "probe", "lawsuit", "delay", "bear")
    bullish_terms = ("buy", "bought", "purchase", "add", "added", "increase", "increased", "raise", "upgrade", "beat", "beats", "rally", "surge", "bull", "holdings", "add")
    if any(term in text for term in bearish_terms):
        return "bearish"
    if any(term in text for term in bullish_terms):
        return "bullish"
    return "neutral"




def _source_event_thesis(symbol: str, sentiment: str, decision: dict[str, Any], title: str) -> str:
    if sentiment == "bullish":
        return f"Source coverage is incrementally positive for {symbol}: {title}"
    if sentiment == "bearish":
        return f"Source coverage raises a downside or invalidation question for {symbol}: {title}"
    reasons = _string_list(decision.get("inclusion_reasons"))
    if reasons:
        return f"{symbol} has a new source item to compare against existing decision evidence: {reasons[0]}"
    return f"{symbol} has a new source item that may affect the watchlist or portfolio review queue."




def _source_event_countercase(symbol: str, sentiment: str, decision: dict[str, Any]) -> str:
    if sentiment == "bullish":
        return "A single positive source item is not enough if valuation, liquidity, or thesis evidence fails to confirm it."
    if sentiment == "bearish":
        return "The negative source item may be transient unless it changes fundamentals, disclosure consensus, or thesis invalidation."
    return _countercase(symbol, decision, {}, {})




def _source_event_next_action(symbols: list[str], portfolio_rows: dict[str, Any], watchlist: set[str]) -> str:
    if any(symbol in portfolio_rows for symbol in symbols):
        return "Check whether this changes sizing, thesis state, or the next portfolio review action."
    if any(symbol in watchlist for symbol in symbols):
        return "Keep on watchlist and require confirming evidence before promotion."
    return "Promote to watchlist only if another independent source confirms the signal."




def _primary_symbol(symbols: list[str], portfolio_rows: dict[str, Any], watchlist: set[str]) -> str:
    for symbol in symbols:
        if symbol in portfolio_rows:
            return symbol
    for symbol in symbols:
        if symbol in watchlist:
            return symbol
    return symbols[0] if symbols else ""




def _provider_source_rows(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT provider, capability, count(*) AS items_count, max(finished_at) AS latest_at
        FROM provider_runs
        GROUP BY provider, capability
        ORDER BY latest_at DESC NULLS LAST
        LIMIT 40
        """,
    )
    return [
        {
            "source_name": f"{row.get('provider')}: {row.get('capability')}",
            "content_type": "provider",
            "items_count": int(row.get("items_count") or 0),
            "tickers_count": 0,
            "latest_at": row.get("latest_at"),
        }
        for row in rows
    ]




def _source_family_for_name(name: str) -> str:
    if "arco" in name or "birdclaw" in name:
        return "thesis"
    if "sec" in name or "filing" in name or "disclosure" in name:
        return "filing"
    if "news" in name:
        return "news"
    if "research" in name:
        return "research"
    if "tradingview" in name:
        return "tradingview"
    if "yfinance" in name:
        return "quote"
    return name.split(":")[0]


# Provider/author names whose news_items rows are long-form blog/newsletter or
# investor-memo content rather than wire news. Sourced from the canonical source
# inventory in core.source_ingestion.definitions; kept here as a small literal so
# the feed read model does not import the (heavier) ingestion package.
_MEMO_SOURCE_HINTS = ("howard marks", "oaktree", "memo", "berkshire", "shareholder letter")
_BLOG_SOURCE_HINTS = (
    "stratechery", "not boring", "semianalysis", "citrini", "capital wars",
    "michael burry", "benedict evans", "chamath", "avc", "substack", "newsletter",
    "blog", "the diff", "net interest",
)


# source_items.source_kind / source_registry.source_family values that carry
# long-form editorial text the feed should surface and let the user navigate.
_LONGFORM_TEXT_FAMILIES = {
    "blog", "newsletter", "memo", "podcast", "transcript",
    "rss_or_blog", "blog_or_social_bridge",
}


def _is_longform_text_family(family: str) -> bool:
    return str(family or "").lower() in _LONGFORM_TEXT_FAMILIES


def _feed_source_family(source_type: str, source_name: str) -> str:
    """Coarse, UI-facing family used to facet the source feed.

    Returns one of: ``filing``, ``thesis``, ``research``, ``memo``, ``blog``,
    ``podcast``, ``transcript``, ``news``. Filings/13F, theses, and research carry
    their own ``source_type``; the ``news`` and long-form (blog/newsletter/…)
    buckets are refined into memo/blog/news by inspecting the provider name.
    """

    kind = str(source_type or "").lower()
    if kind == "filing":
        return "filing"
    if kind in {"thesis", "socials"}:
        return "thesis"
    if kind == "research":
        return "research"
    if kind in {"podcast", "transcript"}:
        return kind
    name = str(source_name or "").lower()
    if kind == "news" or _is_longform_text_family(kind):
        if any(hint in name for hint in _MEMO_SOURCE_HINTS):
            return "memo"
        if any(hint in name for hint in _BLOG_SOURCE_HINTS):
            return "blog"
        return "blog" if _is_longform_text_family(kind) else "news"
    return kind or "news"




def source_rows(source_key: str, title: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _compact_empty_fields(
            {
                "source_key": source_key,
                "title": title,
                "symbol": str(row.get("symbol") or "").upper(),
                "score": row.get("score"),
                "label": row.get("label"),
                "caption": row.get("caption"),
                "source_date": row.get("source_date"),
            }
        )
        for row in rows
        if row.get("symbol")
    ]
