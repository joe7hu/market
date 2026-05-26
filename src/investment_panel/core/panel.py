"""Read models for the FastAPI app."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from investment_panel.core.config import AppConfig, config_to_dict, load_config
from investment_panel.core import brokers
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.daily_brief import daily_brief
from investment_panel.core.decision import canonical_quote_rows, decision_readiness_rows, refresh_decision_read_models
from investment_panel.core.portfolio_intelligence import correlation_edges, exposure_clusters, portfolio_risk_cards, review_actions
from investment_panel.core.research import build_research_packet, generate_deterministic_memo
from investment_panel.core.signals import signal_rows


DECISION_REFRESH_LOCK = Lock()


def load_panel_data(config: dict[str, Any] | AppConfig | None = None) -> dict[str, Any]:
    app_config = config if isinstance(config, AppConfig) else load_config()
    if isinstance(config, dict):
        # FastAPI compatibility path: app.data_access passes a plain dict.
        db_path = Path(config.get("database", {}).get("duckdb_path", "data/investment.duckdb"))
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        config_watchlist = list(config.get("watchlist", []))
    else:
        db_path = app_config.database.duckdb_path
        config_watchlist = app_config.watchlist
    init_db(db_path)
    # Keep the API read connection in the same mode as init/write jobs. DuckDB
    # rejects simultaneous connections to one file when read_only differs.
    with db(db_path, read_only=False) as con:
        decision_refresh = ensure_decision_read_models(con, config_watchlist)
        decision_snapshots = symbol_decision_snapshots(con)
        tables = {
            "signals": signal_rows(con),
            "opportunities_ranked": opportunities_ranked(con),
            "opportunity_sources": opportunity_sources(con),
            "discovered_universe": discovered_universe(con),
            "decision_queue": decision_queue(con),
            "decision_readiness": decision_readiness(con),
            "source_freshness": source_freshness(con),
            "symbol_decision_snapshot": decision_snapshots,
            "symbol_decision_snapshots": decision_snapshots,
            "candidates": candidates(con),
            "portfolio": portfolio(con),
            "theses": theses(con),
            "catalysts": catalysts(con),
            "fundamentals": fundamentals(con),
            "disclosures": disclosures(con),
            "quotes": quotes(con),
            "screener": screener(con),
            "options_expiries": options_expiries(con),
            "options_chain": options_chain(con),
            "options_payoff_scenarios": options_payoff_scenarios(con),
            "news": news(con),
            "tradingview_symbol_search": tradingview_symbol_search(con),
            "tradingview_watchlists": tradingview_watchlists(con),
            "tradingview_alerts": tradingview_alerts(con),
            "tradingview_chart_state": tradingview_chart_state(con),
            "sepa": sepa(con),
            "liquidity": liquidity(con),
            "correlations": correlations(con),
            "etf_premiums": etf_premiums(con),
            "analyst_estimates": analyst_estimates(con),
            "earnings": earnings(con),
            "earnings_setups": earnings_setups(con),
            "valuations": valuations(con),
            "technicals": technicals(con),
            "research_packets": research_packets(con),
            "provider_runs": provider_runs(con),
            "broker_status": brokers.broker_status_rows(con),
            "broker_accounts": brokers.broker_accounts(con),
            "broker_positions": brokers.broker_positions(con),
            "broker_market_snapshots": brokers.broker_market_snapshots(con),
            "broker_scanner_signals": brokers.broker_scanner_signals(con),
            "agent_recommendations": brokers.agent_recommendations(con),
            "paper_orders": brokers.paper_orders(con),
            "daily_brief": daily_brief(con),
            "exposure_clusters": exposure_clusters(con),
            "correlation_edges": correlation_edges(con),
            "portfolio_risk_cards": portfolio_risk_cards(con),
            "review_actions": review_actions(con),
            "ticker_memos": reports(con),
            "trader_twins": trader_profiles(app_config.trader_profile_dir),
            "source_health": source_health(con),
        }
    ready = any(tables[name] for name in ("signals", "candidates", "portfolio", "ticker_memos"))
    return {
        "ready": ready,
        "message": "Loaded investment panel data." if ready else "Database is initialized but contains no screened candidates yet.",
        "source": "duckdb",
        "metadata": {"config": config_to_dict(app_config), "decision_refresh": decision_refresh},
        "tables": tables,
    }


def ensure_decision_read_models(con: Any, config_watchlist: list[dict[str, Any]]) -> dict[str, int | str]:
    counts = query_rows(
        con,
        """
        SELECT
            (SELECT count(*) FROM discovered_universe) AS discovered_universe,
            (SELECT count(*) FROM decision_queue) AS decision_queue,
            (SELECT count(*) FROM source_freshness) AS source_freshness,
            (SELECT count(*) FROM symbol_decision_snapshots) AS symbol_decision_snapshots
        """,
    )[0]
    if all(int(counts.get(key) or 0) > 0 for key in counts):
        return {**counts, "status": "cached"}
    with DECISION_REFRESH_LOCK:
        counts = query_rows(
            con,
            """
            SELECT
                (SELECT count(*) FROM discovered_universe) AS discovered_universe,
                (SELECT count(*) FROM decision_queue) AS decision_queue,
                (SELECT count(*) FROM source_freshness) AS source_freshness,
                (SELECT count(*) FROM symbol_decision_snapshots) AS symbol_decision_snapshots
            """,
        )[0]
        if all(int(counts.get(key) or 0) > 0 for key in counts):
            return {**counts, "status": "cached"}
        result = refresh_decision_read_models(con, config_watchlist)
        return {**result, "status": "refreshed"}


def get_panel_snapshot(config: dict[str, Any] | AppConfig | None = None) -> dict[str, Any]:
    return load_panel_data(config)


def candidates(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT c.run_date, c.symbol, i.name, i.asset_class, i.category,
               c.score AS final_score, c.decision, c.score_breakdown, c.evidence
        FROM candidates c
        LEFT JOIN instruments i ON i.symbol = c.symbol
        QUALIFY row_number() OVER (PARTITION BY c.symbol ORDER BY c.run_date DESC, c.score DESC) = 1
        ORDER BY c.score DESC
        LIMIT 200
        """,
    )
    decoded = [decode_fields(row, ("score_breakdown", "evidence")) for row in rows]
    for row in decoded:
        row["components"] = row.get("score_breakdown") or {}
        evidence = row.get("evidence")
        row["evidence_count"] = len(evidence) if isinstance(evidence, list) else 0
        row["freshness"] = row.get("run_date")
    return decoded


def opportunities_ranked(con: Any) -> list[dict[str, Any]]:
    """Composite opportunity read model used by the workstation UI."""

    decision_rows = decision_queue(con)
    if decision_rows:
        for row in decision_rows:
            row["composite_score"] = row.get("score")
            row["confidence_score"] = confidence_to_number(
                str(row.get("freshness_status") or ""),
                float(row.get("score") or 0),
                int(row.get("evidence_count") or 0),
            )
            basis = row.get("decision_basis") if isinstance(row.get("decision_basis"), dict) else {}
            row["source_counts"] = basis.get("source_counts") or {}
            row["source_count"] = sum(int(value or 0) for value in row["source_counts"].values())
            row["latest_price"] = row.get("latest_quote")
            row["observed_at"] = row.get("latest_quote_at")
            row["top_source"] = row.get("source_cluster")
            row["decision"] = row.get("action_grade")
            row["gates"] = row.get("blocking_gates") or []
        return decision_rows

    source_counts = opportunity_source_counts(con)
    latest_quotes = {
        row["symbol"]: row
        for row in query_rows(
            con,
            """
            SELECT symbol, observed_at, price, change_pct
            FROM quotes_intraday
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY observed_at DESC) = 1
            """,
        )
    }
    ranked = []
    for index, row in enumerate(signal_rows(con), start=1):
        symbol = str(row.get("symbol") or "").upper()
        counts = source_counts.get(symbol, {})
        quote = latest_quotes.get(symbol, {})
        source_count = sum(counts.values())
        components = row.get("components") if isinstance(row.get("components"), dict) else {}
        score = float(row.get("score") or 0)
        confidence = row.get("confidence")
        confidence_score = confidence_to_number(str(confidence or ""), score, source_count)
        ranked.append(
            {
                **row,
                "rank": index,
                "composite_score": score,
                "score": score,
                "confidence_score": confidence_score,
                "source_counts": counts,
                "source_count": source_count,
                "latest_price": quote.get("price"),
                "change_pct": quote.get("change_pct"),
                "observed_at": quote.get("observed_at"),
                "top_source": top_source_label(counts, components),
            }
        )
    return sorted(ranked, key=lambda item: (item.get("score") or 0, item.get("source_count") or 0), reverse=True)


def opportunity_source_counts(con: Any) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}

    def add(source: str, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            symbol = str(row.get("symbol") or row.get("target_symbol") or "").upper()
            if not symbol:
                continue
            counts.setdefault(symbol, {})[source] = int(row.get("count") or 0)

    add("technical", query_rows(con, "SELECT symbol, count(*) AS count FROM technical_features GROUP BY symbol"))
    add("sepa", query_rows(con, "SELECT symbol, count(*) AS count FROM sepa_analyses GROUP BY symbol"))
    add("liquidity", query_rows(con, "SELECT symbol, count(*) AS count FROM liquidity_metrics GROUP BY symbol"))
    add("valuation", query_rows(con, "SELECT symbol, count(*) AS count FROM valuation_models GROUP BY symbol"))
    add("earnings_setup", query_rows(con, "SELECT symbol, count(*) AS count FROM earnings_setups GROUP BY symbol"))
    add("options_payoff", query_rows(con, "SELECT symbol, count(*) AS count FROM options_payoff_scenarios GROUP BY symbol"))
    add("thesis", query_rows(con, "SELECT symbol, count(*) AS count FROM birdclaw_theses GROUP BY symbol"))
    add("filing", query_rows(con, "SELECT symbol, count(*) AS count FROM disclosures WHERE symbol IS NOT NULL GROUP BY symbol"))
    add("earnings", query_rows(con, "SELECT symbol, count(*) AS count FROM earnings_events GROUP BY symbol"))
    return counts


def discovered_universe(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, name, asset_class, inclusion_reasons, source_counts,
               latest_source_timestamp, latest_observed_at, next_event_at,
               eligibility_status, eligibility_detail, evidence_score, discovery_score,
               liquidity_score, recency_score, universe_rank,
               decision_universe_member, updated_at
        FROM discovered_universe
        ORDER BY decision_universe_member DESC, universe_rank ASC, symbol
        LIMIT 1000
        """,
    )
    decoded = [decode_fields(row, ("inclusion_reasons", "source_counts")) for row in rows]
    for row in decoded:
        row["latest_source_at"] = row.get("latest_source_timestamp")
        counts = row.get("source_counts") if isinstance(row.get("source_counts"), dict) else {}
        row["source_count"] = sum(int(value or 0) for key, value in counts.items() if key not in {"config_watchlist", "config", "instrument", "instruments", "candidate"})
        row["total_source_count"] = sum(int(value or 0) for value in counts.values())
    return decoded


def decision_queue(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, rank, action_grade, decision_bucket, score,
               discovery_score, decision_score, action_score,
               freshness_status, quote_freshness, daily_analysis_freshness,
               filing_freshness, thesis_freshness, overall_decision_freshness,
               source_cluster, evidence_count, raw_source_rows, independent_source_count,
               evidence_items_count, primary_evidence_count,
               inclusion_reasons, blocking_gates, decision_basis,
               latest_quote, latest_quote_at, latest_observed_at, next_event_at,
               catalyst_window, liquidity_grade,
               portfolio_impact, invalidation
        FROM decision_queue
        ORDER BY rank ASC, score DESC
        LIMIT 250
        """,
    )
    return [decode_fields(row, ("inclusion_reasons", "blocking_gates", "decision_basis", "portfolio_impact")) for row in rows]


def decision_readiness(con: Any) -> list[dict[str, Any]]:
    return decision_readiness_rows(con)


def source_freshness(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT source_key, source_type, provider, last_observed_at, freshness_status,
               stale_after, status, detail, docs_only, checked_at
        FROM source_freshness
        ORDER BY docs_only ASC, freshness_status DESC, source_key
        """,
    )
    for row in rows:
        row["source"] = row.get("source_key")
        row["source_kind"] = "documentation" if row.get("docs_only") else row.get("source_type")
        row["provider_status"] = row.get("status")
    return rows


def symbol_decision_snapshots(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, action_grade, freshness_status, quote_freshness,
               daily_analysis_freshness, filing_freshness, thesis_freshness, source_cluster,
               inclusion_reasons, blocking_gates, decision_basis, snapshot
        FROM symbol_decision_snapshots
        ORDER BY as_of DESC, symbol
        LIMIT 250
        """,
    )
    decoded = [decode_fields(row, ("inclusion_reasons", "blocking_gates", "decision_basis", "snapshot")) for row in rows]
    for row in decoded:
        snapshot = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
        row["invalidation"] = snapshot.get("invalidation")
    return decoded


def opportunity_sources(con: Any) -> list[dict[str, Any]]:
    """One row per symbol/source leader for the Opportunities source panels."""

    panels: list[dict[str, Any]] = []
    panels.extend(
        source_rows(
            "technical",
            "Technical Setups",
            query_rows(
                con,
                """
                SELECT symbol, as_of AS source_date, score, verdict AS label, stage AS caption
                FROM sepa_analyses
                ORDER BY score DESC NULLS LAST
                LIMIT 50
                """,
            ),
        )
    )
    panels.extend(
        source_rows(
            "liquidity",
            "Liquidity",
            query_rows(
                con,
                """
                SELECT symbol, as_of AS source_date, avg_dollar_volume AS score,
                       grade AS label, 'average dollar volume' AS caption
                FROM liquidity_metrics
                ORDER BY avg_dollar_volume DESC NULLS LAST
                LIMIT 50
                """,
            ),
        )
    )
    panels.extend(
        source_rows(
            "valuation",
            "Valuation",
            query_rows(
                con,
                """
                SELECT symbol, as_of AS source_date, upside_pct AS score,
                       method AS label, 'modeled upside' AS caption
                FROM valuation_models
                ORDER BY upside_pct DESC NULLS LAST
                LIMIT 50
                """,
            ),
        )
    )
    panels.extend(
        source_rows(
            "earnings_setup",
            "Earnings Setups",
            query_rows(
                con,
                """
                SELECT symbol, as_of AS source_date, score,
                       verdict AS label, 'revision/surprise setup' AS caption
                FROM earnings_setups
                ORDER BY score DESC NULLS LAST
                LIMIT 50
                """,
            ),
        )
    )
    panels.extend(
        source_rows(
            "options_payoff",
            "Options Payoff",
            query_rows(
                con,
                """
                SELECT symbol, as_of AS source_date, COALESCE(max_profit, 0) AS score,
                       strategy_type AS label, 'deterministic payoff scenario' AS caption
                FROM options_payoff_scenarios
                ORDER BY as_of DESC, symbol
                LIMIT 50
                """,
            ),
        )
    )
    panels.extend(
        source_rows(
            "thesis",
            "Thesis / Memos",
            query_rows(
                con,
                """
                SELECT symbol, created_at AS source_date, 1 AS score,
                       author AS label, thesis_summary AS caption
                FROM birdclaw_theses
                ORDER BY created_at DESC
                LIMIT 50
                """,
            ),
        )
    )
    panels.extend(
        source_rows(
            "filings",
            "Trader Filings",
            query_rows(
                con,
                """
                SELECT symbol, filed_date AS source_date,
                       TRY_CAST(json_extract(raw, '$.holdings_value_thousands') AS DOUBLE) AS score,
                       coalesce(trader_name, filer_name) AS label, action AS caption
                FROM disclosures
                WHERE symbol IS NOT NULL
                ORDER BY filed_date DESC NULLS LAST
                LIMIT 50
                """,
            ),
        )
    )
    panels.extend(
        source_rows(
            "news",
            "News / Catalysts",
            query_rows(
                con,
                """
                SELECT symbol, event_date AS source_date, 1 AS score,
                       event AS label, expected_impact AS caption
                FROM catalysts
                ORDER BY event_date ASC NULLS LAST
                LIMIT 50
                """,
            ),
        )
    )
    return panels


def source_rows(source_key: str, title: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source_key": source_key,
            "title": title,
            "symbol": str(row.get("symbol") or "").upper(),
            "score": row.get("score"),
            "label": row.get("label"),
            "caption": row.get("caption"),
            "source_date": row.get("source_date"),
        }
        for row in rows
        if row.get("symbol")
    ]


def technicals(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, date, features
        FROM technical_features
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY date DESC) = 1
        ORDER BY date DESC, symbol
        LIMIT 200
        """,
    )
    decoded = [decode_fields(row, ("features",)) for row in rows]
    for row in decoded:
        features = row.get("features") if isinstance(row.get("features"), dict) else {}
        row["close"] = features.get("close")
        row["ma20"] = features.get("ma20")
        row["ma50"] = features.get("ma50")
        row["ma200"] = features.get("ma200")
        row["return_20d"] = features.get("return_20d")
        row["return_60d"] = features.get("return_60d")
        row["technical_score"] = features.get("technical_score")
        row["drawdown_from_high"] = features.get("drawdown_from_high")
        row["volume_ratio_20_60"] = features.get("volume_ratio_20_60")
        row["source"] = features.get("source") or features.get("price_source")
    return decoded


def research_packets(con: Any) -> list[dict[str, Any]]:
    symbols = [
        str(row.get("symbol") or "").upper()
        for row in query_rows(
            con,
            """
            SELECT symbol
            FROM candidates
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY run_date DESC, score DESC) = 1
            ORDER BY score DESC
            LIMIT 25
            """,
        )
    ]
    packets: list[dict[str, Any]] = []
    for symbol in symbols:
        if not symbol:
            continue
        packet = build_research_packet(con, symbol)
        if not packet.get("candidate"):
            continue
        memo = generate_deterministic_memo(packet)
        report = memo.get("json") or {}
        packets.append(
            {
                "symbol": symbol,
                "created_at": packet.get("created_at"),
                "decision": report.get("decision"),
                "conviction": report.get("conviction"),
                "why_now": report.get("why_now"),
                "bull_case": report.get("bull_case"),
                "bear_case": report.get("bear_case"),
                "invalidation": report.get("invalidation"),
                "entry_plan": report.get("entry_plan"),
                "position_sizing": report.get("position_sizing"),
                "portfolio_impact": report.get("portfolio_impact"),
                "evidence_count": len(packet.get("arco_thesis_evidence") or []),
                "price_rows": len(packet.get("prices_recent") or []),
                "has_position": bool(packet.get("portfolio_position")),
            }
        )
    return packets


def confidence_to_number(label: str, score: float, source_count: int) -> int:
    normalized = label.lower()
    if "high" in normalized:
        return 85
    if "medium" in normalized:
        return 65
    if "low" in normalized:
        return 35
    return int(max(20, min(95, score * 0.7 + min(source_count, 8) * 4)))


def top_source_label(counts: dict[str, int], components: dict[str, Any]) -> str:
    if counts:
        return max(counts.items(), key=lambda item: item[1])[0]
    if components:
        return max(components.items(), key=lambda item: float(item[1] or 0))[0]
    return "candidate"


def portfolio(con: Any) -> list[dict[str, Any]]:
    effective_rows = brokers.effective_portfolio_rows(con)
    rows: list[dict[str, Any]] = []
    for item in effective_rows:
        symbol = str(item.get("symbol") or "").upper()
        instrument = query_rows(con, "SELECT name, asset_class, category FROM instruments WHERE symbol = ? LIMIT 1", [symbol])
        meta = instrument[0] if instrument else {}
        rows.append(
            {
                "symbol": symbol,
                "name": meta.get("name") or symbol,
                "asset_class": item.get("asset_class") or meta.get("asset_class"),
                "category": meta.get("category"),
                "quantity": item.get("quantity"),
                "avg_cost": item.get("avg_cost") or item.get("average_cost"),
                "average_cost": item.get("average_cost") or item.get("avg_cost"),
                "purchase_date": item.get("purchase_date"),
                "holding_days": item.get("holding_days"),
                "tax_lot_term": item.get("tax_lot_term") or ("broker" if item.get("source") == "ibkr" else "unknown"),
                "notes": item.get("notes") or "",
                "position_source": item.get("source"),
                "provider": item.get("provider"),
                "account_id": item.get("account_id"),
                "updated_at": item.get("updated_at"),
                "market_price": item.get("market_price"),
                "broker_market_value": item.get("market_value"),
                "broker_unrealized_pnl": item.get("unrealized_pnl"),
            }
        )
    quotes_by_symbol = {str(row.get("symbol") or "").upper(): row for row in canonical_quote_rows(con)}
    decision_by_symbol = {
        str(row.get("symbol") or "").upper(): row
        for row in query_rows(
            con,
            """
            SELECT symbol, action_grade, freshness_status
            FROM decision_queue
            WHERE symbol IN (SELECT symbol FROM portfolio_positions)
            """,
        )
    }
    for row in rows:
        decision = decision_by_symbol.get(str(row.get("symbol") or "").upper(), {})
        action_grade = decision.get("action_grade")
        freshness = decision.get("freshness_status")
        row["signal"] = action_grade
        row["action"] = "Refresh data" if freshness in {"stale", "failed", "missing"} else "Review setup" if action_grade in {"Reject", "Watch", "Research", "Act"} else None
        quote = quotes_by_symbol.get(str(row.get("symbol") or "").upper(), {})
        price = row.get("market_price") or quote.get("price")
        row["price"] = price
        row["change_pct"] = quote.get("change_pct")
        row["change_abs"] = quote.get("change_abs")
        row["quote_source"] = "ibkr" if row.get("position_source") == "ibkr" and row.get("broker_market_value") is not None else quote.get("source")
        row["quote_freshness"] = quote.get("freshness_status")
        if price is None:
            row["market_value"] = row.get("broker_market_value")
            row["unrealized_pnl"] = row.get("broker_unrealized_pnl")
            row["unrealized_pnl_pct"] = None
            continue
        quantity = float(row.get("quantity") or 0)
        avg_cost = float(row.get("avg_cost") or 0)
        row["market_value"] = row.get("broker_market_value") if row.get("broker_market_value") is not None else quantity * float(price)
        row["unrealized_pnl"] = row.get("broker_unrealized_pnl") if row.get("broker_unrealized_pnl") is not None else quantity * (float(price) - avg_cost)
        row["unrealized_pnl_pct"] = ((float(price) - avg_cost) / avg_cost) * 100 if avg_cost > 0 else None
    total_market_value = sum(float(row.get("market_value") or 0) for row in rows if row.get("market_value") is not None)
    for row in rows:
        row["portfolio_weight"] = (float(row["market_value"]) / total_market_value) * 100 if total_market_value and row.get("market_value") is not None else None
    return rows


def theses(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(con, "SELECT symbol, thesis_json, updated_at FROM theses ORDER BY updated_at DESC")
    decoded = [decode_fields(row, ("thesis_json",)) for row in rows]
    if decoded:
        return decoded
    birdclaw_rows = query_rows(
        con,
        """
        SELECT symbol, author, created_at AS updated_at, thesis_summary, claims, engagement, source_url
        FROM birdclaw_theses
        ORDER BY created_at DESC
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("claims", "engagement")) for row in birdclaw_rows]


def catalysts(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        WITH calendar_rows AS (
            SELECT id, symbol, event_date, event, expected_impact, source,
                   start_at, end_at, timezone, event_scope, event_kind, importance,
                   COALESCE(verification_status, 'confirmed') AS verification_status,
                   source_url, source_name, raw
            FROM catalysts
            UNION ALL
            SELECT 'earnings-' || symbol || '-' || CAST(event_date AS TEXT) AS id,
                   symbol,
                   event_date,
                   event_type AS event,
                   'Earnings event from yfinance calendar snapshot' AS expected_impact,
                   source,
                   CAST(NULL AS TIMESTAMP) AS start_at,
                   CAST(NULL AS TIMESTAMP) AS end_at,
                   'America/New_York' AS timezone,
                   'watchlist' AS event_scope,
                   'earnings' AS event_kind,
                   'medium' AS importance,
                   'watch' AS verification_status,
                   CAST(NULL AS TEXT) AS source_url,
                   'yfinance' AS source_name,
                   metrics AS raw
            FROM earnings_events
            UNION ALL
            SELECT 'filing-' || id AS id,
                   symbol,
                   COALESCE(filed_date, event_date) AS event_date,
                   COALESCE(source_type, 'filing') || ' filed' AS event,
                   COALESCE(action, amount, 'Public disclosure filing') AS expected_impact,
                   source_type AS source,
                   CAST(NULL AS TIMESTAMP) AS start_at,
                   CAST(NULL AS TIMESTAMP) AS end_at,
                   'America/New_York' AS timezone,
                   'filing' AS event_scope,
                   'filing' AS event_kind,
                   'medium' AS importance,
                   'confirmed' AS verification_status,
                   source_url,
                   trader_name AS source_name,
                   raw
            FROM disclosures
            WHERE COALESCE(filed_date, event_date) IS NOT NULL
        )
        SELECT *
        FROM calendar_rows
        ORDER BY
            CASE WHEN event_date >= current_date THEN 0 ELSE 1 END,
            CASE WHEN event_date >= current_date THEN event_date END ASC NULLS LAST,
            CASE WHEN event_date < current_date THEN event_date END DESC NULLS LAST,
            start_at ASC NULLS LAST,
            event
        LIMIT 200
        """,
    )
    decoded = [decode_fields(row, ("raw",)) for row in rows]
    return decoded


def fundamentals(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, period_end, filing_date, form_type, metrics, source_url,
               'equity' AS asset_class, 'sec_companyfacts' AS source
        FROM equity_fundamentals
        UNION ALL
        SELECT symbol, date AS period_end, date AS filing_date, 'coingecko_market' AS form_type,
               metrics, source AS source_url, 'crypto' AS asset_class, source
        FROM crypto_fundamentals
        ORDER BY filing_date DESC, symbol
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("metrics",)) for row in rows]


def quotes(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        WITH latest_intraday AS (
            SELECT symbol, observed_at, price, change_pct, change_abs, currency, source, raw,
                   concat(source, ':', symbol) AS freshness_key
            FROM quotes_intraday
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY observed_at DESC) = 1
        ),
        intraday_status AS (
            SELECT i.*, COALESCE(f.freshness_status, 'unknown') AS freshness_status
            FROM latest_intraday i
            LEFT JOIN source_freshness f ON f.source_key = i.freshness_key
        ),
        latest_daily AS (
            SELECT symbol, date AS observed_at, close AS price,
                   CASE WHEN previous_close > 0 THEN ((close - previous_close) / previous_close) * 100 ELSE NULL END AS change_pct,
                   CASE WHEN previous_close IS NOT NULL THEN close - previous_close ELSE NULL END AS change_abs,
                   'USD' AS currency,
                   concat('previous_close:', source) AS source,
                   '{}' AS raw,
                   concat('previous_close:', symbol) AS freshness_key
            FROM (
                SELECT symbol, date, close, source,
                       lag(close) OVER (PARTITION BY symbol ORDER BY date) AS previous_close
                FROM prices_daily
            )
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY date DESC) = 1
        ),
        daily_status AS (
            SELECT d.*, COALESCE(f.freshness_status, 'unknown') AS freshness_status
            FROM latest_daily d
            LEFT JOIN source_freshness f ON f.source_key = d.freshness_key
        ),
        candidates AS (
            SELECT 0 AS priority, symbol, observed_at, price, change_pct, change_abs, currency, source, raw, freshness_status
            FROM intraday_status WHERE freshness_status = 'fresh'
            UNION ALL
            SELECT 1 AS priority, symbol, observed_at, price, change_pct, change_abs, currency, source, raw, freshness_status
            FROM daily_status WHERE freshness_status = 'fresh'
            UNION ALL
            SELECT 2 AS priority, symbol, observed_at, price, change_pct, change_abs, currency, source, raw, freshness_status
            FROM intraday_status WHERE freshness_status <> 'fresh'
            UNION ALL
            SELECT 2 AS priority, symbol, observed_at, price, change_pct, change_abs, currency, source, raw, freshness_status
            FROM daily_status WHERE freshness_status <> 'fresh'
        )
        SELECT symbol, observed_at, price, change_pct, change_abs, currency, source, raw, freshness_status
        FROM candidates
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY priority ASC, observed_at DESC) = 1
        ORDER BY observed_at DESC, symbol
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("raw",)) for row in rows]


def screener(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT run_id, symbol, observed_at, name, metrics, source
        FROM market_screener_rows
        QUALIFY dense_rank() OVER (ORDER BY observed_at DESC) = 1
        ORDER BY observed_at DESC
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("metrics",)) for row in rows]


def options_expiries(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, expiry, dte, contracts_count, observed_at, source, raw
        FROM options_expiries
        ORDER BY observed_at DESC, symbol, expiry
        LIMIT 300
        """,
    )
    return [decode_fields(row, ("raw",)) for row in rows]


def options_chain(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, expiry, strike, option_type, bid, ask, mid, iv, delta, gamma, theta, vega, observed_at, source, raw
        FROM options_chain
        QUALIFY dense_rank() OVER (PARTITION BY symbol, expiry ORDER BY observed_at DESC) = 1
        ORDER BY observed_at DESC, symbol, expiry, strike, option_type
        LIMIT 400
        """,
    )
    return [decode_fields(row, ("raw",)) for row in rows]


def options_payoff_scenarios(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, symbol, as_of, expiry, strategy_type, spot, dte, iv,
               net_premium, max_profit, max_loss, breakevens, legs, curve,
               diagnostics, source
        FROM options_payoff_scenarios
        ORDER BY as_of DESC, symbol, expiry, strategy_type
        LIMIT 300
        """,
    )
    return [decode_fields(row, ("breakevens", "legs", "curve", "diagnostics")) for row in rows]


def news(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, published_at, provider, title, related_symbols, link, source, raw
        FROM news_items
        ORDER BY published_at DESC
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("related_symbols", "raw")) for row in rows]


def tradingview_symbol_search(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, query, observed_at, symbol, description, instrument_type,
               exchange, country, currency, source, raw
        FROM tradingview_symbol_search
        ORDER BY observed_at DESC, query, symbol
        LIMIT 300
        """,
    )
    return [decode_fields(row, ("raw",)) for row in rows]


def tradingview_watchlists(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, observed_at, name, color, symbol_count, symbols, source, raw
        FROM tradingview_watchlists
        ORDER BY observed_at DESC, color NULLS LAST, name
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("symbols", "raw")) for row in rows]


def tradingview_alerts(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, observed_at, name, symbol, alert_type, condition, value,
               active, status, fired_at, source, raw
        FROM tradingview_alerts
        ORDER BY observed_at DESC, fired_at DESC NULLS LAST, symbol
        LIMIT 300
        """,
    )
    return [decode_fields(row, ("raw",)) for row in rows]


def tradingview_chart_state(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, observed_at, layout_id, symbol, interval, url, source, raw
        FROM tradingview_chart_state
        ORDER BY observed_at DESC
        LIMIT 50
        """,
    )
    return [decode_fields(row, ("raw",)) for row in rows]


def sepa(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, score, stage, verdict, checklist, metrics
        FROM sepa_analyses
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY as_of DESC, score DESC NULLS LAST) = 1
        ORDER BY as_of DESC, score DESC NULLS LAST, symbol
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("checklist", "metrics")) for row in rows]


def liquidity(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, grade, avg_daily_volume, avg_dollar_volume,
               turnover_ratio, amihud_illiquidity, impact_1pct_adv_bps, metrics
        FROM liquidity_metrics
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY as_of DESC, avg_dollar_volume DESC NULLS LAST) = 1
        ORDER BY as_of DESC, avg_dollar_volume DESC NULLS LAST, symbol
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("metrics",)) for row in rows]


def correlations(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, target_symbol AS symbol, as_of, lookback_days, peers, metrics
        FROM correlation_runs
        QUALIFY row_number() OVER (PARTITION BY target_symbol ORDER BY as_of DESC) = 1
        ORDER BY as_of DESC, target_symbol
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("peers", "metrics")) for row in rows]


def etf_premiums(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, market_price, nav, premium_pct, metrics, source
        FROM etf_premiums
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY as_of DESC) = 1
        ORDER BY as_of DESC, abs(premium_pct) DESC
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("metrics",)) for row in rows]


def analyst_estimates(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, estimates, source
        FROM analyst_estimates
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY as_of DESC) = 1
        ORDER BY as_of DESC, symbol
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("estimates",)) for row in rows]


def earnings(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, event_date, event_type, metrics, source
        FROM earnings_events
        ORDER BY event_date DESC, symbol
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("metrics",)) for row in rows]


def earnings_setups(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, event_date, setup_type, score, revision_score,
               surprise_score, estimate_spread_score, sentiment_score, verdict,
               metrics, source
        FROM earnings_setups
        QUALIFY dense_rank() OVER (PARTITION BY symbol ORDER BY as_of DESC) = 1
        ORDER BY as_of DESC, score DESC NULLS LAST, symbol
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("metrics",)) for row in rows]


def valuations(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, method, fair_value, upside_pct, assumptions, diagnostics
        FROM valuation_models
        QUALIFY dense_rank() OVER (PARTITION BY symbol ORDER BY as_of DESC) = 1
        ORDER BY as_of DESC, upside_pct DESC NULLS LAST
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("assumptions", "diagnostics")) for row in rows]


def provider_runs(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, provider, capability, started_at, finished_at, status, detail, raw
        FROM provider_runs
        ORDER BY finished_at DESC
        LIMIT 100
        """,
    )
    return [decode_fields(row, ("raw",)) for row in rows]


def disclosures(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        WITH recent_non_13f AS (
            SELECT source_type, trader_name, filer_name, symbol, event_date, filed_date,
                   action, amount, raw, source_url
            FROM disclosures
            WHERE source_type != '13f'
            ORDER BY filed_date DESC NULLS LAST
            LIMIT 200
        ),
        all_13f AS (
            SELECT source_type, trader_name, filer_name, symbol, event_date, filed_date,
                   action, amount, raw, source_url
            FROM disclosures
            WHERE source_type = '13f'
        )
        SELECT source_type, trader_name, filer_name, symbol, event_date, filed_date,
               action, amount, raw, source_url
        FROM recent_non_13f
        UNION ALL
        SELECT source_type, trader_name, filer_name, symbol, event_date, filed_date,
               action, amount, raw, source_url
        FROM all_13f
        ORDER BY filed_date DESC NULLS LAST
        """,
    )
    decoded = [decode_fields(row, ("raw",)) for row in rows]
    enrich_13f_disclosure_rows(decoded)
    for row in decoded:
        raw = row.get("raw") or {}
        if isinstance(raw, dict):
            row["holdings_count"] = raw.get("holdings_count")
            row["holdings_value_thousands"] = raw.get("holdings_value_thousands")
            row["total_value"] = raw.get("total_value")
            row["estimated_invested_usd"] = raw.get("estimated_invested_usd")
            row["performance_percent"] = raw.get("performance_percent")
            row["platform_stats"] = raw.get("platform_stats")
            row["metadata"] = raw.get("metadata")
            row["transactions_count"] = raw.get("transactions_count")
            row["transactions"] = raw.get("transactions")
            row["portfolio_history"] = row.get("portfolio_history") or raw.get("portfolio_history")
            row["sp500_history"] = raw.get("sp500_history")
            row["source_caveat"] = raw.get("source_caveat")
            row["lag_caveat"] = raw.get("lag_caveat")
            row["next_filing_due_date"] = raw.get("next_filing_due_date")
            holdings = raw.get("holdings")
            if isinstance(holdings, list):
                row["holding_sample"] = sorted_13f_holdings(holdings)[:25] if row.get("source_type") == "13f" else holdings[:25]
                trimmed_raw = dict(raw)
                trimmed_raw.pop("holdings", None)
                row["raw"] = trimmed_raw
    return decoded


def enrich_13f_disclosure_rows(rows: list[dict[str, Any]]) -> None:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        if row.get("source_type") != "13f" or not isinstance(raw, dict):
            continue
        key = str(row.get("trader_name") or row.get("filer_name") or raw.get("cik") or "")
        grouped.setdefault(key, []).append(row)

    for group_rows in grouped.values():
        ordered = sorted(group_rows, key=lambda row: str(row.get("event_date") or ""))
        previous_weights: dict[str, float] = {}
        filing_history = []
        for row in ordered:
            raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
            holdings = sorted_13f_holdings(raw.get("holdings") if isinstance(raw, dict) else [])
            current_weights = {holding_key(holding): float(holding.get("weight") or 0.0) for holding in holdings}
            filing_history.append(
                {
                    "date": str(row.get("event_date") or ""),
                    "filed_date": str(row.get("filed_date") or ""),
                    "value": float(raw.get("holdings_value_thousands") or sum(float(holding.get("market_value") or 0.0) for holding in holdings)),
                    "holdings_count": raw.get("holdings_count") or len(holdings),
                }
            )
            history = []
            for holding in holdings[:25]:
                key = holding_key(holding)
                weight = float(holding.get("weight") or 0.0)
                previous = previous_weights.get(key, 0.0)
                history.append(
                    {
                        "symbol": holding.get("symbol"),
                        "security": holding.get("name"),
                        "put_call": holding.get("put_call"),
                        "date": str(row.get("event_date") or ""),
                        "filed_date": str(row.get("filed_date") or ""),
                        "type": "ADD" if previous == 0 and weight > 0 else "INCREASE" if weight > previous else "DECREASE" if weight < previous else "UNCHANGED",
                        "quantity": holding.get("shares_or_principal_amount") or 0,
                        "estimated_amount": float(holding.get("market_value") or 0.0),
                        "price": None,
                        "weight_before": previous,
                        "weight_after": weight,
                    }
                )
            row["allocation_history"] = history
            row["portfolio_history"] = list(filing_history)
            previous_weights = current_weights


def sorted_13f_holdings(holdings: Any) -> list[dict[str, Any]]:
    if not isinstance(holdings, list):
        return []
    total_value = sum(float(row.get("value_thousands") or 0.0) for row in holdings if isinstance(row, dict))
    sorted_rows = []
    for holding in holdings:
        if not isinstance(holding, dict):
            continue
        value = float(holding.get("value_thousands") or 0.0)
        row = dict(holding)
        row["market_value"] = value
        row["weight"] = (value / total_value * 100) if total_value else 0.0
        sorted_rows.append(row)
    return sorted(sorted_rows, key=lambda row: float(row.get("weight") or 0.0), reverse=True)


def holding_key(holding: dict[str, Any]) -> str:
    return ":".join(
        [
            str(holding.get("symbol") or holding.get("cusip") or holding.get("name") or ""),
            str(holding.get("put_call") or ""),
            str(holding.get("title") or ""),
        ]
    )


def reports(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, symbol, created_at, report_type, report_markdown, report_json, evidence
        FROM research_reports
        ORDER BY created_at DESC
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("report_json", "evidence")) for row in rows]


def source_health(con: Any) -> list[dict[str, Any]]:
    return query_rows(con, "SELECT * FROM source_health ORDER BY checked_at DESC")


def trader_profiles(profile_dir: Path) -> list[dict[str, Any]]:
    if not profile_dir.exists():
        return []
    rows = []
    for path in sorted(profile_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        rows.append({"id": path.stem, "name": first_heading(text) or path.stem, "profile_markdown": text})
    return rows


def first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def decode_fields(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    decoded = dict(row)
    for field in fields:
        if field in decoded:
            try:
                decoded[field] = json.loads(decoded[field]) if decoded[field] else None
            except Exception:
                pass
    return decoded
