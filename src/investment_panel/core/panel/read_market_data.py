"""Market-data and quantitative read accessors: quotes, screener, news, fundamentals,
estimates, earnings, valuations, liquidity, correlations, ETF premiums, provider runs."""

from __future__ import annotations
from typing import Any
from investment_panel.core.db import query_rows

from investment_panel.core.panel.coerce import decode_fields
from investment_panel.core.panel.disclosures import _compact_empty_fields



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
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def screener(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT run_id, symbol, observed_at, name, metrics, source
        FROM market_screener_rows
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY observed_at DESC) = 1
        ORDER BY observed_at DESC, symbol
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("metrics",))) for row in rows]




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
    return [_compact_empty_fields(decode_fields(row, ("related_symbols", "raw"))) for row in rows]




def fundamentals(con: Any, symbols: list[str] | set[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    normalized_symbols = sorted({str(symbol or "").upper() for symbol in (symbols or []) if str(symbol or "").strip()})
    where_clause = ""
    params: list[Any] = []
    if normalized_symbols:
        where_clause = f"WHERE upper(symbol) IN ({', '.join(['?'] * len(normalized_symbols))})"
        params.extend(normalized_symbols)
    rows = query_rows(
        con,
        f"""
        SELECT *
        FROM (
            SELECT symbol, period_end, filing_date, form_type, metrics, source_url,
                   'equity' AS asset_class, 'sec_companyfacts' AS source
            FROM equity_fundamentals
            UNION ALL
            SELECT symbol, date AS period_end, date AS filing_date, 'coingecko_market' AS form_type,
                   metrics, source AS source_url, 'crypto' AS asset_class, source
            FROM crypto_fundamentals
        )
        {where_clause}
        ORDER BY filing_date DESC, symbol
        LIMIT 200
        """,
        params,
    )
    return [_compact_empty_fields(decode_fields(row, ("metrics",))) for row in rows]




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
    return [_compact_empty_fields(decode_fields(row, ("metrics",))) for row in rows]




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




def analyst_estimates(con: Any, symbols: list[str] | set[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    normalized_symbols = sorted({str(symbol or "").upper() for symbol in (symbols or []) if str(symbol or "").strip()})
    where_clause = ""
    params: list[Any] = []
    if normalized_symbols:
        where_clause = f"WHERE upper(symbol) IN ({', '.join(['?'] * len(normalized_symbols))})"
        params.extend(normalized_symbols)
    rows = query_rows(
        con,
        f"""
        SELECT symbol, as_of, estimates, source
        FROM analyst_estimates
        {where_clause}
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY as_of DESC) = 1
        ORDER BY as_of DESC, symbol
        LIMIT 200
        """,
        params,
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
        WITH valuation_history AS (
            SELECT symbol, as_of, method, fair_value, upside_pct, assumptions, diagnostics,
                   CASE
                     WHEN count(*) OVER (PARTITION BY symbol, method) > 1
                     THEN (1 - percent_rank() OVER (PARTITION BY symbol, method ORDER BY upside_pct)) * 100
                     ELSE NULL
                   END AS own_history_percentile
            FROM valuation_models
        )
        SELECT symbol, as_of, method, fair_value, upside_pct, assumptions, diagnostics,
               own_history_percentile,
               own_history_percentile AS valuation_percentile_own_history
        FROM valuation_history
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
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def source_health(con: Any) -> list[dict[str, Any]]:
    return query_rows(con, "SELECT * FROM source_health ORDER BY checked_at DESC")
