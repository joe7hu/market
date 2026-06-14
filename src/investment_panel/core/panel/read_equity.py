"""Equity, decision, portfolio, and market-data read accessors."""

from __future__ import annotations
from typing import Any
from investment_panel.core import brokers
from investment_panel.core.db import query_rows
from investment_panel.core.decision import canonical_quote_rows, decision_readiness_rows
from investment_panel.core.signals import signal_rows

from investment_panel.core.panel.coerce import decode_fields, decode_json_value
from investment_panel.core.panel.sources import source_rows
from investment_panel.core.panel.disclosures import _compact_empty_fields



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
        if not evidence:
            evidence = candidate_source_evidence(con, str(row.get("symbol") or ""))
            row["evidence"] = evidence
        row["evidence_count"] = len(evidence) if isinstance(evidence, list) else 0
        row["freshness"] = row.get("run_date")
    return [_compact_empty_fields(row) for row in decoded]




def candidate_source_evidence(con: Any, symbol: str) -> list[dict[str, Any]]:
    if not symbol:
        return []
    return [
        {
            "type": row.get("signal_type") or "source_signal",
            "source_id": row.get("source_id"),
            "summary": row.get("thesis"),
            "observed_at": row.get("observed_at"),
            "evidence_refs": decode_json_value(row.get("evidence_refs")) or [f"source_item:{row.get('source_item_id')}"],
        }
        for row in query_rows(
            con,
            """
            SELECT source_item_id, source_id, observed_at, signal_type, thesis, evidence_refs
            FROM ticker_source_signals
            WHERE upper(symbol) = upper(?)
            ORDER BY observed_at DESC NULLS LAST
            LIMIT 6
            """,
            [symbol],
        )
    ]




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
        return [_compact_empty_fields(row) for row in decision_rows]

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
    sorted_ranked = sorted(ranked, key=lambda item: (item.get("score") or 0, item.get("source_count") or 0), reverse=True)
    return [_compact_empty_fields(row) for row in sorted_ranked]




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
        row["source_count"] = sum(int(value or 0) for key, value in counts.items() if key not in {"config_watchlist", "manual_watchlist", "config", "instrument", "instruments", "candidate"})
        row["total_source_count"] = sum(int(value or 0) for value in counts.values())
        row["next_event_at"] = row.get("next_event_at") or "No upcoming event loaded"
    return [_compact_empty_fields(row) for row in decoded]




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
    decoded = [decode_fields(row, ("inclusion_reasons", "blocking_gates", "decision_basis", "portfolio_impact")) for row in rows]
    for row in decoded:
        row["next_event_at"] = row.get("next_event_at") or "No upcoming event loaded"
    return [_compact_empty_fields(row) for row in decoded]




def decision_readiness(con: Any) -> list[dict[str, Any]]:
    return [_compact_empty_fields(row) for row in decision_readiness_rows(con)]




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
    return [_compact_empty_fields(row) for row in rows]




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
    return [_compact_empty_fields(row) for row in decoded]




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
    return [_compact_empty_fields(row) for row in panels]




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
    return [_compact_empty_fields(row) for row in rows]




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
    return [_compact_empty_fields(row) for row in decoded]
