"""Feed signals, universe screen, and consensus read models."""

from __future__ import annotations
import hashlib
import json
from typing import Any
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.decision import canonical_quote_rows, decision_readiness_rows, effective_watchlist, manual_watchlist_rows, refresh_decision_read_models
from investment_panel.core.sources import ensure_canonical_sources, source_item_rows, source_registry_rows, source_run_rows, source_ticker_ranking_rows, ticker_source_signal_rows
from investment_panel.core.thesis_monitor import thesis_monitor_rows

from investment_panel.core.panel.coerce import _date_text, _dict_from_value, _is_generic_source_signal, _normalize_symbol_token, _number_from_any, _optional_number, _plain_text, _ratio, _string_list, _symbols_from_text, _symbols_from_value
from investment_panel.core.panel.metrics import _free_cash_flow, _is_watch_universe, _metric_number, _metric_number_present, _pe_from_fundamentals, _ps_from_fundamentals, _quality_score, _rank_percentiles, _roic_from_fundamentals, _star_rating, _universe_next_action, _valuation_percentiles_by_symbol, _value_signal, _watch_sort
from investment_panel.core.panel.sources import _countercase, _disclosure_investor_source_rows, _expanded_disclosure_positions, _feed_source_family, _news_provider_source_rows, _primary_symbol, _provider_source_rows, _research_report_source_rows, _source_count_rows, _source_event_countercase, _source_event_next_action, _source_event_thesis, _source_family_counts, _source_family_for_name, _source_sentiment, _thesis_author_source_rows
from investment_panel.core.panel.technicals import technicals
from investment_panel.core.panel.disclosures import _compact_empty_fields, disclosures
from investment_panel.core.panel.read_equity import decision_queue, discovered_universe, portfolio
from investment_panel.core.panel.read_market_data import fundamentals, quotes, screener, valuations



def feed_signals(con: Any, config_watchlist: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """MungerMode-style decision feed, enriched with Joe's portfolio context."""

    watchlist = {str(item.get("symbol") or "").upper() for item in (config_watchlist or []) if item.get("symbol")}
    portfolio_rows = {str(row.get("symbol") or "").upper(): row for row in portfolio(con)}
    decision_rows = decision_queue(con)
    thesis_rows = {str(row.get("symbol") or "").upper(): row for row in thesis_monitor_rows(con, config_watchlist or [])}
    decision_by_symbol = {str(row.get("symbol") or "").upper(): row for row in decision_rows}
    output = _group_feed_events(
        _source_feed_events(con, portfolio_rows, watchlist, decision_by_symbol, thesis_rows),
        portfolio_rows,
        watchlist,
        decision_by_symbol,
    )

    return sorted(output, key=lambda item: (item.get("date") or "", item.get("score") or 0), reverse=True)[:48]




def universe_screen(con: Any, config_watchlist: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Compact watched/candidate ticker screen with quality, value, and action columns."""

    configured_watch = {str(item.get("symbol") or "").upper() for item in (config_watchlist or []) if item.get("symbol")}
    excluded_watch = {
        str(item.get("symbol") or "").upper()
        for item in manual_watchlist_rows(con, include_excluded=True)
        if item.get("watch_state") == "excluded"
    }
    portfolio_symbols = {str(row.get("symbol") or "").upper() for row in portfolio(con)}
    quote_by_symbol = {str(row.get("symbol") or "").upper(): row for row in quotes(con)}
    decision_by_symbol = {str(row.get("symbol") or "").upper(): row for row in decision_queue(con)}
    screener_by_symbol = {str(row.get("symbol") or "").upper(): row for row in screener(con)}
    valuation_percentile_by_symbol = _valuation_percentiles_by_symbol(con, list(screener_by_symbol))
    fundamental_by_symbol: dict[str, dict[str, Any]] = {}
    for row in fundamentals(con):
        symbol = str(row.get("symbol") or "").upper()
        if symbol and symbol not in fundamental_by_symbol:
            fundamental_by_symbol[symbol] = row
    valuation_by_symbol: dict[str, dict[str, Any]] = {}
    for row in valuations(con):
        symbol = str(row.get("symbol") or "").upper()
        if symbol and symbol not in valuation_by_symbol:
            valuation_by_symbol[symbol] = row
    technical_rows = technicals(con)
    technical_by_symbol = {str(row.get("symbol") or "").upper(): row for row in technical_rows}
    rs_rank_1m_by_symbol = _rank_percentiles(technical_rows, "return_20d")
    rs_rank_3m_by_symbol = _rank_percentiles(technical_rows, "return_3m")

    rows = []
    for universe in discovered_universe(con):
        symbol = str(universe.get("symbol") or "").upper()
        if not symbol:
            continue
        decision = decision_by_symbol.get(symbol, {})
        screener_row = screener_by_symbol.get(symbol, {})
        metrics = _dict_from_value(screener_row.get("metrics"))
        fundamental_metrics = _dict_from_value(fundamental_by_symbol.get(symbol, {}).get("metrics"))
        valuation = valuation_by_symbol.get(symbol, {})
        technical = technical_by_symbol.get(symbol, {})
        watch_state = "owned" if symbol in portfolio_symbols else "candidate" if symbol in excluded_watch else "watched" if symbol in configured_watch or _is_watch_universe(universe) else "candidate"
        quality = _quality_score(decision, metrics, valuation)
        forward_pe = _metric_number(metrics, "forward_pe", "forwardPE", "forward_pe_ratio", "pe_forward")
        pe_ratio = _pe_from_fundamentals(metrics, fundamental_metrics)
        ps_ratio = _ps_from_fundamentals(metrics, fundamental_metrics)
        roic = _metric_number(metrics, "roic", "returnOnInvestedCapital", "return_on_invested_capital", "return_on_capital")
        if not roic:
            roic = _roic_from_fundamentals(fundamental_metrics, metrics)
        revenue = _metric_number(metrics, "total_revenue", "totalRevenue", "revenue") or _optional_number(fundamental_metrics.get("revenue"))
        revenue_growth = _metric_number_present(metrics, "revenue_growth", "revenueGrowth")
        if revenue_growth is None:
            revenue_growth = _optional_number(fundamental_metrics.get("revenue_growth"))
        free_cash_flow = _free_cash_flow(metrics, fundamental_metrics)
        fcf_margin = _ratio(free_cash_flow, revenue)
        market_cap = _metric_number(metrics, "market_cap", "marketCap", "market_cap_basic", "market_capitalization")
        fcf_yield = _ratio(free_cash_flow, market_cap)
        valuation_percentile = _optional_number(valuation.get("valuation_percentile_own_history"))
        if valuation_percentile is None:
            valuation_percentile = _optional_number(valuation.get("own_history_percentile"))
        if valuation_percentile is None:
            valuation_percentile = valuation_percentile_by_symbol.get(symbol)
        rows.append(
            {
                "symbol": symbol,
                "name": universe.get("name") or screener_row.get("name") or symbol,
                "watch_state": watch_state,
                "market_cap": market_cap,
                "ps_ratio": ps_ratio,
                "pe_ratio": pe_ratio,
                "forward_pe": forward_pe,
                "forward_pe_source": "provider" if forward_pe else "missing",
                "rev_yoy": revenue_growth,
                "revenue_yoy": revenue_growth,
                "free_cash_flow": free_cash_flow,
                "fcf_yield": fcf_yield,
                "fcf_margin": fcf_margin,
                "roic": roic,
                "roic_source": "provider" if _metric_number(metrics, "roic", "returnOnInvestedCapital", "return_on_invested_capital", "return_on_capital") else "fundamental_proxy" if roic else "missing",
                "rating": _star_rating(quality),
                "quality_score": quality,
                "value_signal": _value_signal(valuation, metrics),
                "valuation_percentile_own_history": valuation_percentile,
                "action": decision.get("action_grade") or "Watch",
                "next_action": _universe_next_action(decision, watch_state),
                "portfolio_relevance": _portfolio_relevance([symbol], {s: {} for s in portfolio_symbols}, configured_watch, decision),
                "freshness": decision.get("freshness_status") or quote_by_symbol.get(symbol, {}).get("freshness_status") or "unknown",
                "price": quote_by_symbol.get(symbol, {}).get("price"),
                "change_pct": quote_by_symbol.get(symbol, {}).get("change_pct"),
                "rs_rank_1m": rs_rank_1m_by_symbol.get(symbol),
                "rs_rank_3m": rs_rank_3m_by_symbol.get(symbol),
                "rs_3m": rs_rank_3m_by_symbol.get(symbol),
                "return_3m": technical.get("return_3m"),
                "relvol_1m": technical.get("rel_volume_1m") or technical.get("relvol_1m"),
                "atr_pct_1m": technical.get("atr_pct_1m"),
                "source_count": universe.get("source_count") or universe.get("total_source_count") or 0,
                "rank": universe.get("universe_rank"),
            }
        )

    sorted_rows = sorted(rows, key=lambda row: (_watch_sort(row), -(float(row.get("quality_score") or 0)), int(row.get("rank") or 9999)))[:500]
    return [_compact_empty_fields(row) for row in sorted_rows]




def source_consensus(con: Any) -> list[dict[str, Any]]:
    """Ninety-day-style source consensus across local/private and public source families."""

    decision_rows = decision_queue(con)
    family_counts = _source_family_counts(decision_rows)
    local_rows: list[dict[str, Any]] = []
    local_rows.extend(_source_count_rows(con, "Arco / Birdclaw", "private_graph", "birdclaw_theses", "symbol", "created_at"))
    local_rows.extend(_source_count_rows(con, "SEC disclosures", "filing", "disclosures", "symbol", "filed_date"))
    local_rows.extend(_source_count_rows(con, "Market research packets", "research", "research_reports", "symbol", "created_at"))
    local_rows.extend(_source_count_rows(con, "News providers", "news", "news_items", "related_symbols", "published_at"))
    local_rows.extend(_news_provider_source_rows(con))
    local_rows.extend(_thesis_author_source_rows(con))
    local_rows.extend(_disclosure_investor_source_rows(con))
    local_rows.extend(_research_report_source_rows(con))
    local_rows.extend(_provider_source_rows(con))

    output: list[dict[str, Any]] = []
    for row in local_rows:
        key = str(row["source_name"]).lower()
        family = _source_family_for_name(key)
        fallback_bullish, fallback_bearish = family_counts.get(family, ([], []))
        bullish = _symbols_from_value(row.get("bullish_symbols")) or fallback_bullish
        bearish = _symbols_from_value(row.get("bearish_symbols")) or fallback_bearish
        output.append(
            {
                **row,
                "is_followed": True,
                "origin": "market",
                "bullish_symbols": bullish[:8],
                "bearish_symbols": bearish[:8],
                "net_consensus": len(bullish) - len(bearish),
                "recommendation": "loaded",
            }
        )

    loaded_names = {str(row["source_name"]).lower() for row in output}
    for registry_row in source_registry_rows(con):
        source_name = str(registry_row.get("source_name") or registry_row.get("source_id") or "")
        if not source_name or source_name.lower() in loaded_names:
            continue
        if not (int(registry_row.get("items_count") or 0) or int(registry_row.get("tickers_count") or 0)):
            continue
        output.append(
            {
                "source_name": source_name,
                "content_type": registry_row.get("source_family") or registry_row.get("source_kind"),
                "items_count": registry_row.get("items_count") or 0,
                "tickers_count": registry_row.get("tickers_count") or 0,
                "latest_at": registry_row.get("latest_run_at"),
                "is_followed": bool(registry_row.get("enabled")),
                "origin": registry_row.get("origin") or "source_registry",
                "bullish_symbols": [],
                "bearish_symbols": [],
                "net_consensus": 0,
                "recommendation": "loaded",
                "freshness": registry_row.get("freshness"),
                "source_id": registry_row.get("source_id"),
            }
        )

    sorted_output = sorted(output, key=lambda row: (row.get("is_followed") is not True, -int(row.get("items_count") or 0), str(row.get("source_name"))))
    return [_compact_empty_fields(row) for row in sorted_output]




def ownership_consensus(con: Any) -> list[dict[str, Any]]:
    """Disclosure consensus by ticker and investor for the superinvestor surface."""

    rows = _expanded_disclosure_positions(disclosures(con))
    by_symbol: dict[str, dict[str, Any]] = {}
    investors: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        investor = str(row.get("trader_name") or row.get("filer_name") or "Tracked investor")
        if investor:
            investor_row = investors.setdefault(investor, {"investor": investor, "holdings": 0, "latest_filed": "", "symbols": set(), "net_buys": 0, "net_sells": 0, "total_value": 0.0})
            investor_row["latest_filed"] = max(str(investor_row["latest_filed"] or ""), str(row.get("filed_date") or ""))
        if not symbol:
            continue
        action = str(row.get("action") or "").lower()
        value = _disclosure_value(row)
        item = by_symbol.setdefault(symbol, {"symbol": symbol, "name": symbol, "holders": set(), "net_buys": 0, "net_sells": 0, "total_value": 0.0, "latest_filed": "", "investors": []})
        item["holders"].add(investor)
        item["total_value"] = float(item["total_value"]) + value
        item["latest_filed"] = max(str(item["latest_filed"] or ""), str(row.get("filed_date") or ""))
        if "sell" in action or "sale" in action or "reduc" in action:
            item["net_sells"] += 1
        elif "buy" in action or "purchase" in action or "add" in action:
            item["net_buys"] += 1
        if investor not in item["investors"]:
            item["investors"].append(investor)
        investors[investor]["symbols"].add(symbol)
        investors[investor]["holdings"] += 1
        investors[investor]["total_value"] = float(investors[investor].get("total_value") or 0) + value
        if "sell" in action or "sale" in action or "reduc" in action:
            investors[investor]["net_sells"] += 1
        elif "buy" in action or "purchase" in action or "add" in action:
            investors[investor]["net_buys"] += 1

    output = []
    for item in by_symbol.values():
        holders = sorted(item.pop("holders"))
        output.append(
            {
                **item,
                "holders": len(holders),
                "holder_names": holders[:8],
                "investors": item.get("investors", [])[:8],
                "net_activity": int(item.get("net_buys") or 0) - int(item.get("net_sells") or 0),
            }
        )

    investor_rows = [
        {
            "source_type": "investor",
            "investor": investor,
            "symbol": "",
            "holders": 0,
            "holder_names": [],
            "investors": [],
            "net_buys": row["net_buys"],
            "net_sells": row["net_sells"],
            "net_activity": int(row["net_buys"]) - int(row["net_sells"]),
            "total_value": row["total_value"],
            "latest_filed": row["latest_filed"],
            "holdings": row["holdings"],
            "symbols": sorted(row["symbols"])[:10],
        }
        for investor, row in investors.items()
    ]
    consensus_rows = sorted(output, key=lambda row: (int(row["holders"]), float(row["total_value"] or 0)), reverse=True)[:250]
    combined = consensus_rows + sorted(investor_rows, key=lambda row: int(row["holdings"]), reverse=True)[:100]
    return [_compact_empty_fields(row) for row in combined]




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

    for row in ticker_source_signal_rows(con, limit=120):
        if str(row.get("source_item_id") or "").startswith(("news:", "arco_thesis:", "disclosure:")):
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




def _fallback_signal_title(symbol: str, category: str) -> str:
    label = category.replace("_", " ").title()
    return f"{symbol} {label}".strip()




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




def _decision_thesis(decision: dict[str, Any], basis: dict[str, Any]) -> str:
    reasons = _string_list(decision.get("inclusion_reasons"))
    if reasons:
        return "; ".join(reasons[:3])
    counts = basis.get("source_counts")
    if isinstance(counts, dict) and counts:
        leaders = ", ".join(f"{key}:{value}" for key, value in list(counts.items())[:4])
        return f"Decision score is supported by source families {leaders}."
    return f"Decision model ranks this at {decision.get('score') or 0} with {decision.get('evidence_count') or 0} evidence rows."




def _decision_evidence(decision: dict[str, Any], basis: dict[str, Any]) -> list[str]:
    evidence = []
    counts = basis.get("source_counts")
    if isinstance(counts, dict):
        evidence.extend(f"{key}: {value}" for key, value in counts.items() if value)
    if decision.get("evidence_count"):
        evidence.append(f"{decision.get('evidence_count')} evidence rows")
    if decision.get("source_cluster"):
        evidence.append(str(decision.get("source_cluster")))
    return evidence[:4]




def _severity_from_decision(decision: dict[str, Any]) -> str:
    grade = str(decision.get("action_grade") or "").lower()
    freshness = str(decision.get("freshness_status") or "").lower()
    if "reject" in grade or "stale" in freshness:
        return "bad"
    if "act" in grade:
        return "good"
    if "research" in grade:
        return "watch"
    return "info"
