"""Per-ticker data-source rows and dossier context."""

from __future__ import annotations
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Iterable

from app.data_access.coerce import _first_row, _latest_row, _number, _object, _text, _text_join, _text_list
from app.data_access.decision_brief import _brief_summary, _is_no_trade_action, _is_option_expired



TICKER_DATA_SOURCE_FAMILIES: tuple[dict[str, Any], ...] = (
    {
        "family": "decision",
        "label": "Decision",
        "tables": ("symbol_decision_snapshot", "decision_queue", "opportunities_ranked", "discovered_universe"),
        "surfaces": ("today", "watchlist", "research", "ticker"),
        "expected_fields": ("action_grade", "freshness_status", "decision_basis", "invalidation"),
    },
    {
        "family": "quote",
        "label": "Quote",
        "tables": ("quotes", "universe_screen"),
        "surfaces": ("today", "watchlist", "portfolio", "ticker"),
        "expected_fields": ("price", "change_pct", "observed_at", "freshness_status"),
    },
    {
        "family": "fundamentals",
        "label": "Fundamentals",
        "tables": ("fundamentals", "universe_screen", "analyst_estimates", "valuations"),
        "surfaces": ("today", "watchlist", "research", "ticker"),
        "expected_fields": ("market_cap", "forward_pe", "fcf_yield", "roic", "metrics"),
    },
    {
        "family": "technical",
        "label": "Technical",
        "tables": ("technicals", "sepa", "liquidity"),
        "surfaces": ("watchlist", "research", "ticker"),
        "expected_fields": ("technical_score", "return_3m", "rel_volume_1m", "atr_pct_1m"),
    },
    {
        "family": "source_evidence",
        "label": "Source Evidence",
        "tables": ("source_consensus", "ticker_source_signals", "feed_signals", "news", "opportunity_sources"),
        "surfaces": ("feed", "sources", "research", "ticker"),
        "expected_fields": ("source_name", "source_id", "title", "sentiment", "observed_at"),
    },
    {
        "family": "thesis",
        "label": "Thesis",
        "tables": ("thesis_monitor", "theses", "research_packets", "memos"),
        "surfaces": ("today", "thesis-monitor", "portfolio", "ticker"),
        "expected_fields": ("thesis", "needs_review", "review_reason", "invalidation"),
    },
    {
        "family": "options",
        "label": "Options",
        "tables": ("options_ticker_signals", "options_expiry_signals", "options_payoff_scenarios", "options_chain", "options_expiries"),
        "surfaces": ("options-radar", "research", "ticker"),
        "expected_fields": ("status", "atm_iv", "expected_move_pct", "nearest_expiry"),
    },
    {
        "family": "ownership",
        "label": "Ownership",
        "tables": ("ownership_consensus", "disclosures"),
        "surfaces": ("superinvestors", "filings", "ticker"),
        "expected_fields": ("holders", "investors", "net_activity", "latest_filed"),
    },
    {
        "family": "portfolio",
        "label": "Portfolio",
        "tables": ("portfolio", "portfolio_risk_cards", "exposure_clusters", "correlation_edges", "review_actions"),
        "surfaces": ("today", "portfolio", "ticker"),
        "expected_fields": ("market_value", "portfolio_weight", "risk_level", "action"),
    },
    {
        "family": "catalysts",
        "label": "Catalysts",
        "tables": ("catalysts", "earnings", "earnings_setups"),
        "surfaces": ("today", "calendar", "ticker"),
        "expected_fields": ("event_date", "event", "event_type", "score", "verdict"),
    },
)




def ticker_data_source_rows(symbol: str, tables: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Summarize ticker data coverage across backend-owned shared surfaces."""

    rows: list[dict[str, Any]] = []
    for spec in TICKER_DATA_SOURCE_FAMILIES:
        table_names = tuple(spec["tables"])
        populated = {name: tables.get(name) or [] for name in table_names if tables.get(name)}
        loaded_rows = [row for table_rows in populated.values() for row in table_rows]
        row_count = len(loaded_rows)
        fields_loaded = _loaded_field_names(loaded_rows)
        expected_fields = tuple(spec["expected_fields"])
        missing_fields = [field for field in expected_fields if field not in fields_loaded]
        status = "loaded" if row_count else "coverage_gap"
        if spec["family"] == "options" and row_count:
            payoff_rows = tables.get("options_payoff_scenarios") or []
            if payoff_rows and all(_is_option_expired(row) for row in payoff_rows):
                status = "expired"
        rows.append(
            {
                "symbol": symbol,
                "family": spec["family"],
                "label": spec["label"],
                "status": status,
                "row_count": row_count,
                "source_tables": list(populated) or list(table_names),
                "shared_surfaces": list(spec["surfaces"]),
                "latest_at": _latest_source_timestamp(loaded_rows) or "not_loaded",
                "fields_loaded": fields_loaded or ["coverage_gap"],
                "missing_fields": missing_fields or ["none"],
                "detail": (
                    f"{row_count} ticker-specific rows loaded from shared read models."
                    if row_count
                    else "No ticker-specific row is loaded for this family; the ticker page uses an explicit coverage-gap row."
                ),
            }
        )
    return rows




def _loaded_field_names(rows: list[dict[str, Any]]) -> list[str]:
    fields: set[str] = set()
    for row in rows:
        for key, value in row.items():
            if key in {"raw", "snapshot", "features", "metrics", "estimates", "decision_basis"}:
                if value not in (None, "", [], {}):
                    fields.add(key)
                continue
            if value not in (None, "", [], {}):
                fields.add(key)
    return sorted(fields)[:16]




def _latest_source_timestamp(rows: list[dict[str, Any]]) -> Any:
    latest = _latest_row(rows, ("observed_at", "as_of", "date", "event_date", "updated_at", "created_at", "latest_at", "filing_date", "period_end"))
    for key in ("observed_at", "as_of", "date", "event_date", "updated_at", "created_at", "latest_at", "filing_date", "period_end"):
        if latest.get(key):
            return latest[key]
    return None




def _ensure_ticker_dossier_tables(symbol: str, tables: dict[str, list[dict[str, Any]]]) -> None:
    """Guarantee every ticker dossier tab has backend-owned context rows."""

    decision = _first_row(tables, "symbol_decision_snapshot", "symbol_decision_snapshots", "decision_queue", "opportunities_ranked", "discovered_universe", "universe_screen")
    universe = _first_row(tables, "universe_screen", "discovered_universe")
    quote = _latest_row(tables.get("quotes") or [], ("observed_at", "as_of", "date"))

    if not any(tables.get(key) for key in ("quotes", "technicals", "sepa", "liquidity", "valuations")):
        tables["quotes"] = [_ticker_price_context(symbol, decision, universe)]
    elif not quote and universe:
        tables.setdefault("quotes", []).append(_ticker_price_context(symbol, decision, universe))

    if not any(tables.get(key) for key in ("fundamentals", "analyst_estimates", "earnings", "earnings_setups", "valuations")):
        tables["fundamentals"] = [_ticker_fundamental_context(symbol, decision, universe)]

    if not tables.get("source_consensus"):
        tables["source_consensus"] = [_ticker_source_context(symbol, decision, universe)]

    if not any(tables.get(key) for key in ("disclosures", "ownership_consensus")):
        tables["ownership_consensus"] = [_ticker_ownership_context(symbol)]

    if not tables.get("feed_signals"):
        tables["feed_signals"] = [_ticker_feed_context(symbol, decision, universe)]

    if not any(tables.get(key) for key in ("theses", "thesis_monitor", "memos", "research_packets")):
        tables["thesis_monitor"] = [_ticker_thesis_context(symbol, decision, universe)]




def _ticker_price_context(symbol: str, decision: dict[str, Any], universe: dict[str, Any]) -> dict[str, Any]:
    price = _number(universe.get("price") or decision.get("latest_quote"))
    return {
        "symbol": symbol,
        "source": "ticker_dossier_coverage",
        "observed_at": universe.get("latest_observed_at") or decision.get("latest_quote_at") or decision.get("as_of"),
        "price": price or None,
        "change_pct": universe.get("change_pct"),
        "freshness_status": decision.get("quote_freshness") or universe.get("freshness") or "not_loaded",
        "summary": "Price context from watchlist, universe, and decision evidence." if price else "No current price is available; treat this as a coverage gap before acting.",
    }




def _ticker_fundamental_context(symbol: str, decision: dict[str, Any], universe: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "form_type": "ticker_dossier_coverage",
        "source": "universe_screen",
        "filing_date": decision.get("as_of") or universe.get("updated_at"),
        "metrics": {
            "market_cap": universe.get("market_cap"),
            "pe_ratio": universe.get("pe_ratio"),
            "forward_pe": universe.get("forward_pe"),
            "forward_pe_source": universe.get("forward_pe_source"),
            "roic": universe.get("roic"),
            "roic_source": universe.get("roic_source"),
            "quality_score": universe.get("quality_score"),
            "value_signal": universe.get("value_signal"),
            "watch_state": universe.get("watch_state"),
        },
        "summary": "Fundamental context synthesized from watchlist evidence because direct fundamentals are not available for this ticker.",
    }




def _ticker_source_context(symbol: str, decision: dict[str, Any], universe: dict[str, Any]) -> dict[str, Any]:
    basis = _object(decision.get("decision_basis"))
    counts = _object(basis.get("source_counts"))
    return {
        "source_name": "Ticker source coverage",
        "source": "decision_read_model",
        "symbol": symbol,
        "content_type": "coverage",
        "items_count": int(sum(_number(value) for value in counts.values())) if counts else int(_number(universe.get("source_count"))),
        "tickers_count": 1,
        "bullish_symbols": [symbol] if not _is_no_trade_action(decision.get("action_grade")) else [],
        "bearish_symbols": [symbol] if _is_no_trade_action(decision.get("action_grade")) else [],
        "ticker_history": [{"symbols": [symbol], "title": basis.get("summary") or decision.get("source_cluster") or "No per-source history loaded.", "date": decision.get("as_of")}],
        "recommendation": "coverage_gap" if not counts else "loaded",
    }




def _ticker_ownership_context(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "source_type": "coverage_gap",
        "source": "ownership_consensus",
        "holders": 0,
        "net_buys": 0,
        "net_sells": 0,
        "total_value": 0,
        "summary": "No mapped disclosure or ownership consensus row is loaded for this ticker.",
    }




def _ticker_feed_context(symbol: str, decision: dict[str, Any], universe: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"coverage:{symbol}",
        "symbol": symbol,
        "symbols": [symbol],
        "source": "ticker_dossier_coverage",
        "source_type": "coverage",
        "date": decision.get("as_of") or universe.get("updated_at"),
        "title": f"{symbol} coverage row",
        "thesis": _brief_summary(symbol, decision, _object(decision.get("decision_basis")), _text_list(decision.get("blocking_gates"))),
        "antithesis": decision.get("invalidation") or "No ticker-specific countercase has been promoted yet.",
        "portfolio_relevance": _text(_object(decision.get("portfolio_impact")).get("summary")) or "Review against Joe's portfolio/watchlist before action.",
        "next_action": decision.get("catalyst_window") or "Open the thesis tab and fill any missing evidence families.",
        "freshness": decision.get("freshness_status") or universe.get("freshness") or "not_loaded",
    }




def _ticker_thesis_context(symbol: str, decision: dict[str, Any], universe: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "source": "ticker_dossier_coverage",
        "status": universe.get("watch_state") or "candidate",
        "thesis": _text_join(decision.get("inclusion_reasons")) or "No explicit thesis row is loaded yet.",
        "why_owned_watched": universe.get("portfolio_relevance") or "Ticker is present in the investment universe.",
        "invalidation": decision.get("invalidation") or "Define the countercase before changing exposure.",
        "needs_review": True,
        "review_reason": "Needs review because this dossier has coverage context but no stored thesis.",
        "evidence_links": [],
        "last_reviewed": decision.get("as_of") or universe.get("updated_at"),
    }
