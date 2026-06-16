"""Source-feed event construction and grouping for the decision feed.

Builds normalized feed events from each source surface (news, theses, filings,
research, long-form blog/memo source items, and canonical ticker signals), then
groups duplicates into ticker-aware cards. Consumed by ``feed_signals`` in the
panel feed facade.
"""

from __future__ import annotations
import hashlib
import json
from typing import Any

from investment_panel.core.db import query_rows
from investment_panel.core.sources import source_item_rows, ticker_source_signal_rows

from investment_panel.core.panel.coerce import _date_text, _dict_from_value, _is_generic_source_signal, _normalize_symbol_token, _number_from_any, _plain_text, _string_list, _symbols_from_text, _symbols_from_value
from investment_panel.core.panel.disclosures import _compact_empty_fields, disclosures
from investment_panel.core.panel.sources import _countercase, _expanded_disclosure_positions, _feed_source_family, _is_longform_text_family, _primary_symbol, _source_event_countercase, _source_event_next_action, _source_event_thesis, _source_sentiment


def _disclosure_value(row: dict[str, Any]) -> float:
    raw = _dict_from_value(row.get("raw"))
    for value in (
        row.get("total_value"),
        row.get("holdings_value_thousands"),
        row.get("estimated_invested_usd"),
        row.get("amount_mid"),
        raw.get("amount_mid"),
        raw.get("value_usd"),
        raw.get("estimated_invested_usd"),
        raw.get("holdings_value_thousands"),
        row.get("amount"),
        raw.get("amount_raw"),
    ):
        parsed = _number_from_any(value)
        if parsed:
            return parsed
    return 0.0


def _source_feed_events(
    con: Any,
    portfolio_rows: dict[str, Any],
    watchlist: set[str],
    decision_by_symbol: dict[str, dict[str, Any]],
    thesis_rows: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    known_symbols = set(decision_by_symbol) | set(portfolio_rows) | watchlist
    for row in query_rows(
        con,
        """
        SELECT id, published_at, provider, title, related_symbols, link, source
        FROM news_items
        ORDER BY published_at DESC
        LIMIT 80
        """,
    ):
        title = str(row.get("title") or "Source update")
        symbols = _symbols_from_value(row.get("related_symbols")) or _symbols_from_text(title, known_symbols)
        if not symbols:
            continue
        primary = _primary_symbol(symbols, portfolio_rows, watchlist)
        sentiment = _source_sentiment(title, "")
        decision = decision_by_symbol.get(primary, {})
        events.append(
            {
                "id": f"news:{row.get('id')}",
                "date": _date_text(row.get("published_at")),
                "source": str(row.get("provider") or row.get("source") or "News"),
                "source_type": "news",
                "source_family": _feed_source_family("news", str(row.get("provider") or row.get("source") or "")),
                "sentiment": sentiment,
                "title": title,
                "symbols": symbols,
                "primary_symbol": primary,
                "thesis": _source_event_thesis(primary, sentiment, decision, title),
                "antithesis": _source_event_countercase(primary, sentiment, decision_by_symbol.get(primary, {})),
                "evidence": [title, str(row.get("link") or "")],
                "portfolio_relevance": _portfolio_relevance(symbols, portfolio_rows, watchlist, decision),
                "next_action": _source_event_next_action(symbols, portfolio_rows, watchlist),
                "freshness": "current",
                "severity": "good" if sentiment == "bullish" else "bad" if sentiment == "bearish" else "info",
                "score": 62 if sentiment != "neutral" else 48,
            }
        )

    for row in query_rows(
        con,
        """
        SELECT id, symbol, author, created_at, thesis_summary, claims, source_url
        FROM birdclaw_theses
        ORDER BY created_at DESC
        LIMIT 60
        """,
    ):
        symbol = _normalize_symbol_token(row.get("symbol"))
        if not symbol:
            continue
        claims = _dict_from_value(row.get("claims"))
        evidence = claims.get("evidence") if isinstance(claims.get("evidence"), list) else []
        first_evidence = next((item for item in evidence if isinstance(item, dict)), {})
        decision = decision_by_symbol.get(symbol, {})
        thesis_text = _plain_text(claims.get("text")) or str(row.get("thesis_summary") or "Arco/Birdclaw evidence raised this ticker.")
        title = str(row.get("thesis_summary") or f"{symbol} thesis evidence")
        events.append(
            {
                "id": f"thesis:{row.get('id')}",
                "feed_group_key": _feed_group_key_from_parts("socials", row.get("created_at") or first_evidence.get("date"), title, thesis_text or first_evidence.get("text")),
                "date": _date_text(row.get("created_at") or first_evidence.get("date")),
                "source": str(row.get("author") or "Arco / Birdclaw"),
                "source_type": "socials",
                "source_family": "thesis",
                "sentiment": _source_sentiment(title, thesis_text),
                "title": title,
                "symbols": [symbol],
                "primary_symbol": symbol,
                "thesis": thesis_text,
                "antithesis": _countercase(symbol, decision, thesis_rows.get(symbol, {}), {}),
                "evidence": [str(first_evidence.get("text") or row.get("source_url") or "")],
                "portfolio_relevance": _portfolio_relevance([symbol], portfolio_rows, watchlist, decision),
                "next_action": "Convert the source claim into a thesis/invalidation update if it affects Joe's watchlist.",
                "freshness": str(decision.get("freshness_status") or "current"),
                "severity": "watch",
                "score": 68,
            }
        )

    for row in _expanded_disclosure_positions(disclosures(con))[:120]:
        symbol = _normalize_symbol_token(row.get("symbol"))
        if not symbol:
            continue
        source_type = str(row.get("source_type") or "")
        if "13f" not in source_type and "disclosure" not in source_type:
            continue
        investor = str(row.get("trader_name") or row.get("filer_name") or "Tracked investor")
        action = str(row.get("action") or "disclosed")
        decision = decision_by_symbol.get(symbol, {})
        sentiment = _source_sentiment("", action)
        events.append(
            {
                "id": f"filing:{investor}:{symbol}:{row.get('filed_date')}:{action}",
                "feed_group_key": _feed_group_key_from_parts("filing", row.get("filed_date") or row.get("event_date"), investor, action, row.get("source_url") or row.get("id")),
                "date": _date_text(row.get("filed_date") or row.get("event_date")),
                "source": investor,
                "source_type": "filing",
                "source_family": "filing",
                "sentiment": sentiment,
                "title": f"{investor} {action.lower()} disclosed positions",
                "action": action,
                "symbols": [symbol],
                "primary_symbol": symbol,
                "thesis": f"{investor} disclosure adds ownership evidence for {symbol}.",
                "antithesis": "Disclosure data is delayed and may not represent the investor's current position or intent.",
                "evidence": [str(row.get("source_url") or ""), f"value {_disclosure_value(row):,.0f}" if _disclosure_value(row) else ""],
                "portfolio_relevance": _portfolio_relevance([symbol], portfolio_rows, watchlist, decision),
                "next_action": "Use as consensus evidence only after checking price, thesis, and filing lag.",
                "freshness": str(decision.get("freshness_status") or "current"),
                "severity": "good" if sentiment == "bullish" else "bad" if sentiment == "bearish" else "info",
                "score": 56,
            }
        )

    for row in query_rows(
        con,
        """
        SELECT id, symbol, created_at, report_type, report_json
        FROM research_reports
        ORDER BY created_at DESC
        LIMIT 40
        """,
    ):
        symbol = _normalize_symbol_token(row.get("symbol"))
        if not symbol or symbol == "PORTFOLIO":
            continue
        report = _dict_from_value(row.get("report_json"))
        decision = decision_by_symbol.get(symbol, {})
        events.append(
            {
                "id": f"research:{row.get('id')}",
                "date": _date_text(row.get("created_at")),
                "source": "Market research packet",
                "source_type": "research",
                "source_family": "research",
                "sentiment": "neutral",
                "title": f"{symbol} {str(row.get('report_type') or 'research').replace('_', ' ')}",
                "symbols": [symbol],
                "primary_symbol": symbol,
                "thesis": _plain_text(report.get("why_now")) or "Deterministic research packet has new evidence for this ticker.",
                "antithesis": _plain_text(report.get("bear_case") or report.get("invalidation")) or _countercase(symbol, decision, thesis_rows.get(symbol, {}), {}),
                "evidence": _string_list(report.get("why_now"))[:2] + _string_list(report.get("bull_case"))[:2],
                "portfolio_relevance": _portfolio_relevance([symbol], portfolio_rows, watchlist, decision),
                "next_action": _plain_text(report.get("entry_plan")) or "Review the ticker dossier before changing exposure.",
                "freshness": str(decision.get("freshness_status") or "current"),
                "severity": "info",
                "score": 58,
            }
        )

    # Long-form editorial text (blogs, investor memos, newsletters, podcast and
    # transcript notes) is stored in the canonical source_items table rather than
    # news_items, so surface it directly and classify it into navigable families.
    emitted_source_item_ids: set[str] = set()
    for row in source_item_rows(con, limit=120):
        family = str(row.get("source_family") or row.get("source_kind") or "")
        if not _is_longform_text_family(family):
            continue
        title = str(row.get("title") or "Source update")
        symbols = _symbols_from_value(row.get("tickers")) or _symbols_from_text(title, known_symbols)
        if not symbols:
            continue
        item_id = str(row.get("id") or "")
        if item_id:
            emitted_source_item_ids.add(item_id)
        source_name = str(row.get("source_name") or row.get("source_id") or "Source")
        coarse_family = _feed_source_family(family, source_name)
        summary = _plain_text(row.get("summary")) or title
        primary = _primary_symbol(symbols, portfolio_rows, watchlist)
        sentiment = _source_sentiment(title, summary)
        decision = decision_by_symbol.get(primary, {})
        evidence = _string_list(row.get("evidence_refs"))
        events.append(
            {
                "id": f"source_item:{item_id}",
                "feed_group_key": _feed_group_key_from_parts("source_item", item_id),
                "source_item_id": item_id,
                "date": _date_text(row.get("published_at") or row.get("observed_at")),
                "source": source_name,
                "source_type": coarse_family,
                "source_family": coarse_family,
                "sentiment": sentiment,
                "title": title,
                "symbols": symbols,
                "primary_symbol": primary,
                "thesis": summary,
                "antithesis": _source_event_countercase(primary, sentiment, decision),
                "evidence": [item for item in (evidence + [str(row.get("url") or "")]) if item],
                "portfolio_relevance": _portfolio_relevance(symbols, portfolio_rows, watchlist, decision),
                "next_action": _source_event_next_action(symbols, portfolio_rows, watchlist),
                "freshness": str(decision.get("freshness_status") or "current"),
                "severity": "good" if sentiment == "bullish" else "bad" if sentiment == "bearish" else "info",
                "score": 60,
            }
        )

    for row in ticker_source_signal_rows(con, limit=120):
        if str(row.get("source_item_id") or "").startswith(("news:", "arco_thesis:", "disclosure:")):
            continue
        if str(row.get("source_item_id") or "") in emitted_source_item_ids:
            continue
        if _is_generic_source_signal(row):
            continue
        symbol = _normalize_symbol_token(row.get("symbol"))
        if not symbol:
            continue
        decision = decision_by_symbol.get(symbol, {})
        title = str(row.get("title") or row.get("thesis") or "Source signal")
        evidence = _string_list(row.get("evidence_refs"))
        events.append(
            {
                "id": f"source_signal:{row.get('id')}",
                "feed_group_key": _feed_group_key_from_parts("source_signal", row.get("source_item_id") or row.get("id"), row.get("source_id")),
                "source_item_id": row.get("source_item_id"),
                "date": _date_text(row.get("observed_at")),
                "source": str(row.get("source_name") or row.get("source_id") or "Source signal"),
                "source_type": str(row.get("signal_type") or "source_signal"),
                "source_family": _feed_source_family(str(row.get("signal_type") or "source_signal"), str(row.get("source_name") or row.get("source_id") or "")),
                "sentiment": str(row.get("sentiment") or "neutral"),
                "title": title,
                "symbols": [symbol],
                "primary_symbol": symbol,
                "thesis": str(row.get("thesis") or _source_event_thesis(symbol, str(row.get("sentiment") or "neutral"), decision, title)),
                "antithesis": str(row.get("antithesis") or _source_event_countercase(symbol, str(row.get("sentiment") or "neutral"), decision)),
                "evidence": evidence or [title],
                "portfolio_relevance": _portfolio_relevance([symbol], portfolio_rows, watchlist, decision),
                "next_action": "Refresh market context before action." if row.get("needs_market_context") else _source_event_next_action([symbol], portfolio_rows, watchlist),
                "freshness": "needs_market_context" if row.get("needs_market_context") else str(decision.get("freshness_status") or "current"),
                "severity": "good" if row.get("sentiment") == "bullish" else "bad" if row.get("sentiment") == "bearish" else "info",
                "score": 58 if not row.get("needs_market_context") else 42,
            }
        )
    return events


def _group_feed_events(
    events: list[dict[str, Any]],
    portfolio_rows: dict[str, Any],
    watchlist: set[str],
    decision_by_symbol: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for event in events:
        symbols = _symbols_from_value(event.get("symbols"))
        primary_symbol = _normalize_symbol_token(event.get("primary_symbol"))
        if primary_symbol and primary_symbol not in symbols:
            symbols.insert(0, primary_symbol)
        if not symbols:
            continue

        key = _feed_event_group_key(event)
        group = grouped.setdefault(
            key,
            {
                **event,
                "symbols": [],
                "ticker_contexts": [],
                "feed_item_count": 0,
                "source_count": 0,
                "_sources": [],
                "_evidence": [],
                "_context_keys": set(),
            },
        )
        group["feed_item_count"] = int(group.get("feed_item_count") or 0) + 1
        group["score"] = max(float(group.get("score") or 0), float(event.get("score") or 0))
        group["date"] = max(str(group.get("date") or ""), str(event.get("date") or ""))
        group["severity"] = _feed_more_severe(str(group.get("severity") or ""), str(event.get("severity") or ""))
        _append_unique(group["_sources"], str(event.get("source") or "Source"))
        for item in _string_list(event.get("evidence")):
            _append_unique(group["_evidence"], item)
        for symbol in symbols:
            _append_unique(group["symbols"], symbol)
            context = {
                "symbol": symbol,
                "source": event.get("source"),
                "thesis": event.get("thesis"),
                "antithesis": event.get("antithesis"),
                "portfolio_relevance": event.get("portfolio_relevance"),
                "next_action": event.get("next_action"),
                "freshness": event.get("freshness"),
                "severity": event.get("severity"),
                "sentiment": event.get("sentiment"),
            }
            context_key = json.dumps(context, sort_keys=True, default=str)
            if context_key not in group["_context_keys"]:
                group["_context_keys"].add(context_key)
                group["ticker_contexts"].append(_compact_empty_fields(context))

    output: list[dict[str, Any]] = []
    for key, group in grouped.items():
        symbols = list(group.get("symbols") or [])
        primary_symbol = _primary_symbol(symbols, portfolio_rows, watchlist)
        sources = list(group.get("_sources") or [])
        evidence = list(group.get("_evidence") or [])
        source_type = str(group.get("source_type") or "")
        if sources:
            group["source"] = sources[0] if len(sources) == 1 else f"{sources[0]} +{len(sources) - 1}"
        group["sources"] = sources
        group["source_count"] = len(sources)
        group["evidence"] = evidence[:6]
        group["symbols"] = symbols
        group["ticker_count"] = len(symbols)
        group["primary_symbol"] = primary_symbol
        if int(group.get("feed_item_count") or 0) > 1:
            group["id"] = f"feed_group:{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"
        if len(symbols) > 1:
            group["portfolio_relevance"] = _portfolio_relevance(symbols, portfolio_rows, watchlist, decision_by_symbol.get(primary_symbol, {}))
            group["next_action"] = _source_event_next_action(symbols, portfolio_rows, watchlist)
            if source_type == "filing":
                action = str(group.get("action") or "disclosed").lower()
                group["title"] = f"{group.get('source') or 'Tracked investor'} {action} {len(symbols)} positions"
                group["thesis"] = f"{group.get('source') or 'Tracked investor'} disclosure adds ownership evidence across {len(symbols)} tickers."
            elif source_type not in {"socials", "news"} and int(group.get("feed_item_count") or 0) > 1:
                group["thesis"] = f"This source item maps to {len(symbols)} tickers: {', '.join(symbols[:8])}{'...' if len(symbols) > 8 else ''}."
        group.pop("feed_group_key", None)
        group.pop("_sources", None)
        group.pop("_evidence", None)
        group.pop("_context_keys", None)
        output.append(_compact_empty_fields(group))
    return output


def _feed_event_group_key(event: dict[str, Any]) -> str:
    explicit = str(event.get("feed_group_key") or "")
    if explicit:
        return explicit
    source_item_id = str(event.get("source_item_id") or "")
    if source_item_id:
        return _feed_group_key_from_parts("source_item", source_item_id)
    return _feed_group_key_from_parts(
        event.get("source_type"),
        event.get("date"),
        event.get("title"),
        event.get("source"),
        event.get("thesis"),
    )


def _feed_group_key_from_parts(*parts: Any) -> str:
    normalized = [_plain_text(part).lower() for part in parts if _plain_text(part)]
    return "feed:" + "|".join(normalized)


def _feed_more_severe(left: str, right: str) -> str:
    ranks = {"bad": 4, "bearish": 4, "sell": 4, "warn": 3, "watch": 3, "good": 2, "bullish": 2, "info": 1, "neutral": 1}
    return left if ranks.get(left, 0) >= ranks.get(right, 0) else right


def _append_unique(values: list[Any], value: Any) -> None:
    if value not in values:
        values.append(value)


def _portfolio_relevance(symbols: list[str], portfolio_rows: dict[str, Any], watchlist: set[str], decision: dict[str, Any]) -> str:
    owned = [symbol for symbol in symbols if symbol in portfolio_rows]
    watched = [symbol for symbol in symbols if symbol in watchlist and symbol not in owned]
    impact = _dict_from_value(decision.get("portfolio_impact"))
    if owned:
        if impact:
            return str(impact.get("summary") or impact.get("impact") or f"Owned exposure: {', '.join(owned[:4])}")
        return f"Owned exposure: {', '.join(owned[:4])}"
    if watched:
        return f"Watchlist impact: {', '.join(watched[:4])}"
    if symbols:
        return f"Candidate impact: compare {', '.join(symbols[:4])} against Joe's owned and watched names."
    return "Portfolio impact not yet tied to a ticker."
