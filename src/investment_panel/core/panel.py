"""Read models for the FastAPI app."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from investment_panel.core.config import AppConfig, config_to_dict, load_config
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.research import build_research_packet, generate_deterministic_memo
from investment_panel.core.signals import signal_rows


def load_panel_data(config: dict[str, Any] | AppConfig | None = None) -> dict[str, Any]:
    app_config = config if isinstance(config, AppConfig) else load_config()
    if isinstance(config, dict):
        # FastAPI compatibility path: app.data_access passes a plain dict.
        db_path = Path(config.get("database", {}).get("duckdb_path", "data/investment.duckdb"))
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
    else:
        db_path = app_config.database.duckdb_path
    init_db(db_path)
    # Keep the API read connection in the same mode as init/write jobs. DuckDB
    # rejects simultaneous connections to one file when read_only differs.
    with db(db_path, read_only=False) as con:
        tables = {
            "signals": signal_rows(con),
            "opportunities_ranked": opportunities_ranked(con),
            "opportunity_sources": opportunity_sources(con),
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
            "news": news(con),
            "sepa": sepa(con),
            "liquidity": liquidity(con),
            "correlations": correlations(con),
            "etf_premiums": etf_premiums(con),
            "analyst_estimates": analyst_estimates(con),
            "earnings": earnings(con),
            "valuations": valuations(con),
            "technicals": technicals(con),
            "research_packets": research_packets(con),
            "provider_runs": provider_runs(con),
            "ticker_memos": reports(con),
            "trader_twins": trader_profiles(app_config.trader_profile_dir),
            "source_health": source_health(con),
        }
    ready = any(tables[name] for name in ("signals", "candidates", "portfolio", "ticker_memos"))
    return {
        "ready": ready,
        "message": "Loaded investment panel data." if ready else "Database is initialized but contains no screened candidates yet.",
        "source": "duckdb",
        "metadata": {"config": config_to_dict(app_config)},
        "tables": tables,
    }


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
    add("thesis", query_rows(con, "SELECT symbol, count(*) AS count FROM birdclaw_theses GROUP BY symbol"))
    add("filing", query_rows(con, "SELECT symbol, count(*) AS count FROM disclosures WHERE symbol IS NOT NULL GROUP BY symbol"))
    add("earnings", query_rows(con, "SELECT symbol, count(*) AS count FROM earnings_events GROUP BY symbol"))
    return counts


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
    symbols = [str(row.get("symbol") or "").upper() for row in opportunities_ranked(con)[:25]]
    packets: list[dict[str, Any]] = []
    for symbol in symbols:
        if not symbol:
            continue
        packet = build_research_packet(con, symbol)
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
    return query_rows(
        con,
        """
        SELECT p.symbol, i.name, i.asset_class, i.category, p.quantity,
               p.avg_cost, p.avg_cost AS average_cost,
               p.purchase_date,
               CASE
                   WHEN p.purchase_date IS NULL THEN NULL
                   ELSE date_diff('day', p.purchase_date, current_date)
               END AS holding_days,
               CASE
                   WHEN p.purchase_date IS NULL THEN 'unknown'
                   WHEN date_diff('day', p.purchase_date, current_date) > 365 THEN 'long_term'
                   ELSE 'short_term'
               END AS tax_lot_term,
               q.price,
               CASE WHEN q.price IS NOT NULL THEN p.quantity * q.price ELSE NULL END AS market_value,
               CASE WHEN q.price IS NOT NULL THEN p.quantity * (q.price - p.avg_cost) ELSE NULL END AS unrealized_pnl,
               CASE
                   WHEN q.price IS NOT NULL AND p.avg_cost > 0 THEN ((q.price - p.avg_cost) / p.avg_cost) * 100
                   ELSE NULL
               END AS unrealized_pnl_pct,
               p.notes
        FROM portfolio_positions p
        LEFT JOIN instruments i ON i.symbol = p.symbol
        LEFT JOIN (
            SELECT symbol, price
            FROM quotes_intraday
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY observed_at DESC) = 1
        ) q ON q.symbol = p.symbol
        ORDER BY p.symbol
        """,
    )


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
    rows = query_rows(con, "SELECT * FROM catalysts ORDER BY event_date ASC NULLS LAST LIMIT 200")
    decoded = [decode_fields(row, ("raw",)) for row in rows]
    if decoded:
        return decoded
    earnings_rows = query_rows(
        con,
        """
        SELECT 'earnings-' || symbol || '-' || CAST(event_date AS TEXT) AS id,
               symbol,
               event_date,
               event_type AS event,
               'Earnings event from yfinance calendar snapshot' AS expected_impact,
               source,
               metrics AS raw
        FROM earnings_events
        ORDER BY event_date ASC, symbol
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("raw",)) for row in earnings_rows]


def fundamentals(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, period_end, filing_date, form_type, metrics, source_url
        FROM equity_fundamentals
        ORDER BY filing_date DESC, symbol
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("metrics",)) for row in rows]


def quotes(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, observed_at, price, change_pct, change_abs, currency, source, raw
        FROM quotes_intraday
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY observed_at DESC) = 1
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


def sepa(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, score, stage, verdict, checklist, metrics
        FROM sepa_analyses
        ORDER BY score DESC
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
        ORDER BY avg_dollar_volume DESC NULLS LAST
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
        ORDER BY as_of DESC, symbol
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


def valuations(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, method, fair_value, upside_pct, assumptions, diagnostics
        FROM valuation_models
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
        SELECT source_type, trader_name, filer_name, symbol, event_date, filed_date,
               action, amount, raw, source_url
        FROM disclosures
        ORDER BY filed_date DESC NULLS LAST
        LIMIT 200
        """,
    )
    decoded = [decode_fields(row, ("raw",)) for row in rows]
    for row in decoded:
        raw = row.get("raw") or {}
        if isinstance(raw, dict):
            row["holdings_count"] = raw.get("holdings_count")
            row["holdings_value_thousands"] = raw.get("holdings_value_thousands")
            row["lag_caveat"] = raw.get("lag_caveat")
            holdings = raw.get("holdings")
            if isinstance(holdings, list):
                row["holding_sample"] = holdings[:25]
                trimmed_raw = dict(raw)
                trimmed_raw.pop("holdings", None)
                row["raw"] = trimmed_raw
    return decoded


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
