"""Option symbol/expiry selection and chain filtering."""

from __future__ import annotations
from typing import Any
from investment_panel.core.config import AppConfig
from investment_panel.core.db import json_dumps, query_rows
from investment_panel.core.decision import effective_watchlist

from investment_panel.core.free_sources.constants import OPTION_SCAN_LIMIT, RADAR_CALL_STRIKE_OTM_HI, RADAR_CALL_STRIKE_OTM_LO, RADAR_MAX_DTE, RADAR_MAX_EXPIRIES_PER_SYMBOL, RADAR_MIN_DTE, RADAR_STRIKES_AROUND_SPOT
from investment_panel.core.free_sources.coerce import _dte_from_expiry, _radar_expiry_targets, _unique_strings, as_float, as_int, normalize_symbol, unique_symbols



def latest_tradingview_option_chain_expiries(con: Any, symbols: list[str] | None = None) -> list[dict[str, Any]]:
    target_symbols = unique_symbols(symbols or [])
    symbol_filter = ""
    params: list[Any] = ["tradingview"]
    if target_symbols:
        placeholders = ", ".join(["?"] * len(target_symbols))
        symbol_filter = f"AND oc.symbol IN ({placeholders})"
        params.extend(target_symbols)
    return query_rows(
        con,
        f"""
        SELECT oc.symbol, oc.expiry, max(oc.observed_at) AS observed_at, count(*) AS chain_rows
        FROM options_chain oc
        WHERE oc.source = ?
          {symbol_filter}
          AND oc.observed_at = (
              SELECT max(latest.observed_at)
              FROM options_chain latest
              WHERE latest.symbol = oc.symbol AND latest.source = oc.source
          )
        GROUP BY oc.symbol, oc.expiry
        ORDER BY oc.symbol, oc.expiry
        """,
        params,
    )




def equity_symbols(con: Any) -> list[str]:
    rows = query_rows(con, "SELECT symbol FROM instruments WHERE asset_class IN ('equity', 'etf') ORDER BY symbol")
    return [row["symbol"] for row in rows]




def tradingview_symbol_candidates(symbol: str) -> list[str]:
    normalized = symbol.upper()
    if ":" in normalized:
        return [normalized]
    exchange_overrides = {
        "SPY": ["AMEX:SPY", "NYSEARCA:SPY", "SPY"],
        "QQQ": ["NASDAQ:QQQ", "AMEX:QQQ", "QQQ"],
    }
    if normalized in exchange_overrides:
        return exchange_overrides[normalized]
    return [normalized, f"NASDAQ:{normalized}", f"NYSE:{normalized}", f"AMEX:{normalized}"]




def option_symbols(con: Any, config: AppConfig) -> list[str]:
    configured = [symbol.upper() for symbol in config.data_sources.tradingview.options_symbols]
    if configured:
        return configured
    scan_limit = option_scan_limit(config)
    watchlist = [
        str(item.get("symbol") or "").upper()
        for item in effective_watchlist(con, getattr(config, "watchlist", []) or [])
        if item.get("symbol") and str(item.get("asset_class") or "").lower() in {"equity", "etf"}
    ]
    ranked_budget = max(1, scan_limit - len(watchlist))
    convexity_budget = max(5, scan_limit // 4)
    convexity_rows = convexity_option_symbols(con, min(convexity_budget, ranked_budget))
    decision_limit = max(1, ranked_budget - len(convexity_rows))
    decision_rows = query_rows(
        con,
        """
        SELECT symbol
        FROM decision_queue
        WHERE symbol NOT LIKE '%-USD'
          AND upper(COALESCE(action_grade, '')) NOT IN ('REJECT', 'STALE')
        ORDER BY rank NULLS LAST,
                 action_score DESC NULLS LAST,
                 decision_score DESC NULLS LAST,
                 score DESC NULLS LAST
        LIMIT ?
        """,
        [decision_limit],
    )
    candidate_limit = max(1, ranked_budget - len(decision_rows) - len(convexity_rows))
    candidate_rows = query_rows(
        con,
        """
        SELECT symbol
        FROM candidates
        WHERE symbol NOT LIKE '%-USD'
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY run_date DESC, score DESC) = 1
        ORDER BY score DESC
        LIMIT ?
        """,
        [candidate_limit],
    )
    selected = unique_symbols(
        [
            *watchlist,
            *[row["symbol"] for row in decision_rows],
            *convexity_rows,
            *[row["symbol"] for row in candidate_rows],
            *equity_symbols(con),
        ]
    )
    return selected[: max(scan_limit, len(watchlist))]


def convexity_option_symbols(con: Any, limit: int) -> list[str]:
    """Rank symbols where cheap-convexity option coverage is most likely useful.

    This is a collection-universe screen, not an investment recommendation. It
    favors high recent realized volatility, large 1-3 month moves, post-drawdown
    rebounds, SEPA setups, and minimum dollar-volume support, then lets the radar
    strategies price/gate the actual contracts.
    """

    rows = query_rows(
        con,
        """
        WITH latest_technical AS (
            SELECT symbol, features
            FROM technical_features
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY date DESC) = 1
        ),
        latest_liquidity AS (
            SELECT symbol, avg_dollar_volume
            FROM liquidity_metrics
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY as_of DESC) = 1
        ),
        latest_sepa AS (
            SELECT symbol, score AS sepa_score, stage, verdict
            FROM sepa_analyses
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY as_of DESC) = 1
        ),
        priced AS (
            SELECT
                symbol,
                date,
                close,
                lag(close) OVER (PARTITION BY symbol ORDER BY date) AS prev_close,
                row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS recency_rank
            FROM prices_daily
        ),
        price_stats AS (
            SELECT
                symbol,
                stddev_samp(
                    CASE
                        WHEN recency_rank <= 64 AND prev_close > 0
                        THEN close / prev_close - 1
                        ELSE NULL
                    END
                ) * sqrt(252) AS realized_vol_63d
            FROM priced
            GROUP BY symbol
        ),
        scored AS (
            SELECT
                i.symbol,
                (
                    greatest(COALESCE(TRY_CAST(json_extract(tf.features, '$.return_20d') AS DOUBLE), 0.0), 0.0) * 90.0
                    + greatest(COALESCE(TRY_CAST(json_extract(tf.features, '$.return_60d') AS DOUBLE), 0.0), 0.0) * 55.0
                    + COALESCE(ps.realized_vol_63d, 0.0) * 35.0
                    + CASE
                        WHEN TRY_CAST(json_extract(tf.features, '$.drawdown_from_high') AS DOUBLE) <= -0.25
                        THEN abs(TRY_CAST(json_extract(tf.features, '$.drawdown_from_high') AS DOUBLE)) * 70.0
                        ELSE 0.0
                      END
                    + CASE
                        WHEN lower(COALESCE(ls.verdict, '')) LIKE '%strong%' OR lower(COALESCE(ls.stage, '')) LIKE '%stage_2%'
                        THEN 20.0
                        ELSE 0.0
                      END
                    + COALESCE(ls.sepa_score, 0.0) * 0.10
                ) AS convexity_score,
                COALESCE(ll.avg_dollar_volume, 0.0) AS avg_dollar_volume
            FROM instruments i
            LEFT JOIN latest_technical tf ON tf.symbol = i.symbol
            LEFT JOIN latest_liquidity ll ON ll.symbol = i.symbol
            LEFT JOIN latest_sepa ls ON ls.symbol = i.symbol
            LEFT JOIN price_stats ps ON ps.symbol = i.symbol
            WHERE i.asset_class IN ('equity', 'etf')
              AND i.symbol NOT LIKE '%-USD'
        )
        SELECT symbol
        FROM scored
        WHERE convexity_score > 0
          AND avg_dollar_volume >= 5000000
        ORDER BY convexity_score DESC, avg_dollar_volume DESC, symbol
        LIMIT ?
        """,
        [max(1, int(limit))],
    )
    return [str(row["symbol"]).upper() for row in rows if row.get("symbol")]




def option_scan_limit(config: AppConfig) -> int:
    value = getattr(config.data_sources.tradingview, "option_scan_limit", OPTION_SCAN_LIMIT)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return OPTION_SCAN_LIMIT




def selected_option_expiries(
    rows: list[dict[str, Any]],
    observed_at: str,
    *,
    radar_min_dte: int = RADAR_MIN_DTE,
    radar_max_dte: int = RADAR_MAX_DTE,
    max_radar_expiries: int = RADAR_MAX_EXPIRIES_PER_SYMBOL,
) -> list[str]:
    """Pick near-term coverage plus representative LEAP expiries for the radar."""

    normalized = sorted(
        (row for row in (_expiry_row(row, observed_at) for row in rows) if row is not None),
        key=lambda row: int(row["dte"]),
    )
    if not normalized:
        return []
    selected: list[str] = [str(normalized[0]["expiry"])]
    radar_rows = [row for row in normalized if radar_min_dte <= int(row["dte"]) <= radar_max_dte]
    if radar_rows and max_radar_expiries > 0:
        targets = _radar_expiry_targets(radar_min_dte, radar_max_dte, max_radar_expiries)
        remaining = radar_rows[:]
        for target in targets:
            if not remaining:
                break
            best = min(remaining, key=lambda row: abs(int(row["dte"]) - target))
            selected.append(str(best["expiry"]))
            remaining = [row for row in remaining if row["expiry"] != best["expiry"]]
    return _unique_strings(selected)




def option_chain_strikes_around_spot(
    expiry: str,
    rows: list[dict[str, Any]],
    observed_at: str,
    *,
    configured: int,
    radar_min_dte: int = RADAR_MIN_DTE,
    radar_max_dte: int = RADAR_MAX_DTE,
    radar_strikes_around_spot: int = RADAR_STRIKES_AROUND_SPOT,
) -> int:
    """Use wider LEAP strike sampling for 10x radar math without widening near-term chains."""

    normalized = [_expiry_row(row, observed_at) for row in rows]
    match = next((row for row in normalized if row and str(row["expiry"]) == str(expiry)), None)
    dte = int(match["dte"]) if match else None
    if dte is not None and radar_min_dte <= dte <= radar_max_dte:
        return max(configured, radar_strikes_around_spot)
    return configured




def _expiry_row(row: dict[str, Any], observed_at: str) -> dict[str, Any] | None:
    expiry = row.get("expiry")
    if not expiry:
        return None
    dte = as_int(row.get("dte"))
    if dte is None:
        dte = _dte_from_expiry(str(expiry), observed_at)
    if dte is None:
        return None
    return {"expiry": str(expiry), "dte": dte}




def tradingview_search_symbols(con: Any, config: AppConfig) -> list[str]:
    configured = [symbol.upper() for symbol in config.data_sources.tradingview.search_symbols]
    if configured:
        return unique_symbols(configured)[:25]
    watchlist = [str(item.get("symbol") or "").upper() for item in config.watchlist if item.get("symbol")]
    candidate_rows = query_rows(
        con,
        """
        SELECT symbol
        FROM candidates
        WHERE symbol NOT LIKE '%-USD'
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY run_date DESC, score DESC) = 1
        ORDER BY score DESC
        LIMIT 15
        """,
    )
    return unique_symbols([*watchlist, *[row["symbol"] for row in candidate_rows]])[:25]




def latest_option_scan_spot(con: Any, symbol: str) -> float | None:
    normalized = normalize_symbol(symbol)
    quote_rows = query_rows(
        con,
        """
        SELECT price
        FROM quotes_intraday
        WHERE symbol = ?
        ORDER BY observed_at DESC
        LIMIT 1
        """,
        [normalized],
    )
    if quote_rows:
        price = as_float(quote_rows[0].get("price"))
        if price is not None and price > 0:
            return price
    price_rows = query_rows(
        con,
        """
        SELECT close
        FROM prices_daily
        WHERE symbol = ?
        ORDER BY date DESC
        LIMIT 1
        """,
        [normalized],
    )
    if price_rows:
        price = as_float(price_rows[0].get("close"))
        if price is not None and price > 0:
            return price
    return None




def filter_chain_rows_around_spot(rows: list[dict[str, Any]], spot: float | None, strikes_around_spot: int) -> list[dict[str, Any]]:
    if spot is None or spot <= 0 or strikes_around_spot <= 0:
        return rows
    max_rows_per_type = max(1, int(strikes_around_spot)) * 2 + 1
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        option_type = str(row.get("type") or row.get("option_type") or "").lower()
        expiry = str(row.get("expiry") or "")
        grouped.setdefault((expiry, option_type), []).append(row)
    filtered: list[dict[str, Any]] = []
    for (_expiry, option_type), grouped_rows in grouped.items():
        with_strikes = [(as_float(row.get("strike")), row) for row in grouped_rows]
        usable = [(strike, row) for strike, row in with_strikes if strike is not None]
        if not usable:
            filtered.extend(grouped_rows)
            continue
        if option_type == "call" and strikes_around_spot >= RADAR_STRIKES_AROUND_SPOT:
            band = [
                (strike, row)
                for strike, row in usable
                if RADAR_CALL_STRIKE_OTM_LO * spot <= strike <= RADAR_CALL_STRIKE_OTM_HI * spot
            ]
            if band:
                if len(band) <= strikes_around_spot:
                    filtered.extend(row for _strike, row in sorted(band, key=lambda item: float(item[0])))
                    continue
                step = (len(band) - 1) / (strikes_around_spot - 1) if strikes_around_spot > 1 else 0
                picked = {round(index * step) for index in range(strikes_around_spot)}
                filtered.extend(row for _strike, row in sorted((band[index] for index in picked), key=lambda item: float(item[0])))
                continue
        nearest = sorted(usable, key=lambda item: abs(float(item[0]) - spot))[:max_rows_per_type]
        filtered.extend(row for _strike, row in sorted(nearest, key=lambda item: float(item[0])))
    return filtered
