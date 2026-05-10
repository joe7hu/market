"""Read models for the FastAPI app."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from investment_panel.core.config import AppConfig, config_to_dict, load_config
from investment_panel.core.db import db, init_db, query_rows
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
    return [decode_fields(row, ("score_breakdown", "evidence")) for row in rows]


def portfolio(con: Any) -> list[dict[str, Any]]:
    return query_rows(
        con,
        """
        SELECT p.symbol, i.name, i.asset_class, i.category, p.quantity, p.avg_cost, p.notes
        FROM portfolio_positions p
        LEFT JOIN instruments i ON i.symbol = p.symbol
        ORDER BY p.symbol
        """,
    )


def theses(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(con, "SELECT symbol, thesis_json, updated_at FROM theses ORDER BY updated_at DESC")
    return [decode_fields(row, ("thesis_json",)) for row in rows]


def catalysts(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(con, "SELECT * FROM catalysts ORDER BY event_date ASC NULLS LAST LIMIT 200")
    return [decode_fields(row, ("raw",)) for row in rows]


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
