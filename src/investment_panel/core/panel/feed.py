"""Feed signals, universe screen, and consensus read models."""

from __future__ import annotations
from typing import Any
from investment_panel.core.decision import manual_watchlist_rows
from investment_panel.core.sources import source_registry_rows
from investment_panel.core.thesis_monitor import thesis_monitor_rows

from investment_panel.core.panel.coerce import _dict_from_value, _optional_number, _ratio, _symbols_from_value
from investment_panel.core.panel.metrics import _free_cash_flow, _is_watch_universe, _metric_number, _metric_number_present, _pe_from_fundamentals, _ps_from_fundamentals, _quality_score, _rank_percentiles, _roic_from_fundamentals, _star_rating, _universe_next_action, _valuation_percentiles_by_symbol, _value_signal, _watch_sort
from investment_panel.core.panel.sources import _disclosure_investor_source_rows, _expanded_disclosure_positions, _news_provider_source_rows, _provider_source_rows, _research_report_source_rows, _source_count_rows, _source_family_counts, _source_family_for_name, _thesis_author_source_rows
from investment_panel.core.panel.feed_events import _disclosure_value, _group_feed_events, _portfolio_relevance, _source_feed_events
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


