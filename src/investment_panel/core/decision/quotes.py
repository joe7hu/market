"""Canonical quote read model."""

from __future__ import annotations
from typing import Any
from investment_panel.core.db import json_dumps, query_rows, upsert_instrument



def canonical_quote_rows(con: Any) -> list[dict[str, Any]]:
    """Return one decision quote per symbol using the same priority as the UI.

    A fresh previous close can satisfy quote freshness after the equity market
    closes, so the displayed decision price must come from that same source
    instead of an older intraday row.
    """

    return query_rows(
        con,
        """
        WITH latest_intraday AS (
            SELECT symbol, observed_at, price, change_pct, change_abs, currency, source,
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
            SELECT 0 AS priority, * FROM intraday_status WHERE freshness_status = 'fresh'
            UNION ALL
            SELECT 1 AS priority, * FROM daily_status WHERE freshness_status = 'fresh'
            UNION ALL
            SELECT 2 AS priority, * FROM intraday_status WHERE freshness_status <> 'fresh'
            UNION ALL
            SELECT 2 AS priority, * FROM daily_status WHERE freshness_status <> 'fresh'
        )
        SELECT symbol, observed_at, price, change_pct, change_abs, currency, source, freshness_status
        FROM candidates
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY priority ASC, observed_at DESC) = 1
        ORDER BY symbol
        """,
    )
