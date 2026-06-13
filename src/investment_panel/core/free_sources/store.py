"""Persistence of quotes, chains, screener, news, and TradingView rows."""

from __future__ import annotations
from datetime import date, datetime
from typing import Any
from investment_panel.core.db import json_dumps, query_rows

from investment_panel.core.free_sources.coerce import as_float, as_int, normalize_symbol, parse_json_object, stable_id



def store_yfinance_options_liquidity(
    con: Any,
    symbol: str,
    expiry: str,
    liquidity_observed_at: str,
    chain_observed_at: Any,
    rows: list[dict[str, Any]],
) -> int:
    by_contract: dict[tuple[float, str], dict[str, Any]] = {}
    for row in rows:
        strike = as_float(row.get("strike"))
        option_type = str(row.get("type") or row.get("option_type") or "").lower()
        if strike is None or option_type not in {"call", "put"}:
            continue
        by_contract[(strike, option_type)] = row
    if not by_contract:
        return 0
    chain_rows = query_rows(
        con,
        """
        SELECT symbol, expiry, strike, option_type, observed_at, source, raw
        FROM options_chain
        WHERE symbol = ?
          AND expiry = TRY_CAST(? AS DATE)
          AND observed_at = TRY_CAST(? AS TIMESTAMP)
          AND source = 'tradingview'
        """,
        [symbol.upper(), expiry, str(chain_observed_at)],
    )
    updated = 0
    for chain in chain_rows:
        strike = as_float(chain.get("strike"))
        option_type = str(chain.get("option_type") or "").lower()
        if strike is None:
            continue
        liquidity = by_contract.get((strike, option_type))
        if not liquidity:
            continue
        volume = as_int(liquidity.get("volume"))
        open_interest = as_int(liquidity.get("open_interest") if liquidity.get("open_interest") is not None else liquidity.get("openInterest"))
        if volume is None and open_interest is None:
            continue
        raw = parse_json_object(chain.get("raw"))
        if volume is not None:
            raw["volume"] = volume
        if open_interest is not None:
            raw["open_interest"] = open_interest
            raw["openInterest"] = open_interest
        last = as_float(liquidity.get("last"))
        if last is not None:
            raw.setdefault("last", last)
        raw["liquidity_source"] = "yfinance"
        raw["liquidity_observed_at"] = liquidity_observed_at
        raw["liquidity_contract_symbol"] = liquidity.get("contract_symbol") or liquidity.get("contractSymbol")
        con.execute(
            """
            UPDATE options_chain
            SET raw = ?
            WHERE symbol = ?
              AND expiry = ?
              AND strike = ?
              AND option_type = ?
              AND observed_at = ?
              AND source = ?
            """,
            [
                json_dumps(raw),
                chain["symbol"],
                chain["expiry"],
                chain["strike"],
                chain["option_type"],
                chain["observed_at"],
                chain["source"],
            ],
        )
        updated += 1
    return updated




def store_yfinance_market_snapshot(con: Any, run_id: str, symbol: str, observed_at: str, info: dict[str, Any]) -> bool:
    market_cap = as_float(info.get("marketCap"))
    if market_cap is None or market_cap <= 0:
        return False
    metrics = {
        "market_cap_basic": market_cap,
        "market_cap": market_cap,
        "shares_outstanding": as_float(info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")),
        "regular_market_price": as_float(info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose")),
        "previous_close": as_float(info.get("previousClose")),
        "trailing_pe": as_float(info.get("trailingPE")),
        "forward_pe": as_float(info.get("forwardPE")),
        "peg_ratio": as_float(info.get("pegRatio")),
        "price_to_sales": as_float(info.get("priceToSalesTrailing12Months")),
        "price_to_book": as_float(info.get("priceToBook")),
        "total_revenue": as_float(info.get("totalRevenue")),
        "revenue_growth": as_float(info.get("revenueGrowth")),
        "net_margin": as_float(info.get("profitMargins")),
        "operating_cash_flow": as_float(info.get("operatingCashflow") or info.get("totalCashFromOperatingActivities")),
        "capital_expenditures": as_float(info.get("capitalExpenditures") or info.get("capital_expenditures")),
        "free_cash_flow": as_float(info.get("freeCashflow") or info.get("free_cash_flow")),
        "total_cash": as_float(info.get("totalCash")),
        "total_debt": as_float(info.get("totalDebt")),
        "quote_type": info.get("quoteType"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "source": "yfinance_info",
    }
    con.execute(
        """
        INSERT OR REPLACE INTO market_screener_rows
        (run_id, symbol, observed_at, name, metrics, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            run_id,
            str(symbol).upper(),
            observed_at,
            info.get("shortName") or info.get("longName") or str(symbol).upper(),
            json_dumps(metrics),
            "yfinance_info",
        ],
    )
    return True




def update_instrument_from_yfinance(con: Any, symbol: str, info: dict[str, Any]) -> None:
    name = info.get("shortName") or info.get("longName")
    sector = info.get("sector")
    industry = info.get("industry")
    quote_type = str(info.get("quoteType") or "").upper()
    asset_class = "etf" if quote_type == "ETF" else "equity"
    con.execute(
        """
        UPDATE instruments
        SET name = CASE WHEN ? IS NOT NULL AND ? != '' AND (name IS NULL OR name = '' OR name = symbol) THEN ? ELSE name END,
            asset_class = COALESCE(NULLIF(asset_class, ''), ?),
            sector = COALESCE(NULLIF(sector, ''), ?),
            industry = COALESCE(NULLIF(industry, ''), ?)
        WHERE symbol = ?
        """,
        [name, name, name, asset_class, sector, industry, str(symbol).upper()],
    )




def upsert_quote(con: Any, symbol: str, observed_at: str, row: dict[str, Any]) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO quotes_intraday
        (symbol, observed_at, price, change_pct, change_abs, currency, source, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            normalize_symbol(row.get("symbol") or symbol),
            row.get("time") or observed_at,
            as_float(row.get("close")),
            as_float(row.get("change")),
            as_float(row.get("change_abs")),
            row.get("currency"),
            "tradingview",
            json_dumps(row),
        ],
    )




def store_screener_rows(con: Any, run_id: str, observed_at: str, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        metrics = {key: value for key, value in row.items() if key not in {"symbol", "name"}}
        con.execute(
            """
            INSERT OR REPLACE INTO market_screener_rows
            (run_id, symbol, observed_at, name, metrics, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [run_id, symbol, observed_at, row.get("name"), json_dumps(metrics), "tradingview"],
        )




def store_expiries(con: Any, symbol: str, observed_at: str, rows: list[dict[str, Any]], *, source: str = "tradingview") -> int:
    count = 0
    normalized_symbol = normalize_symbol(symbol)
    for row in rows:
        expiry = row.get("expiry")
        if not expiry:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO options_expiries
            (symbol, expiry, dte, contracts_count, observed_at, source, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [normalized_symbol, expiry, as_int(row.get("dte")), as_int(row.get("contracts_count")), observed_at, source, json_dumps(row)],
        )
        count += 1
    return count




def store_options_chain(con: Any, symbol: str, observed_at: str, rows: list[dict[str, Any]], *, source: str = "tradingview") -> int:
    count = 0
    normalized_symbol = normalize_symbol(symbol)
    for row in rows:
        expiry = row.get("expiry")
        strike = as_float(row.get("strike"))
        option_type = str(row.get("type") or row.get("option_type") or "").lower()
        if not expiry or strike is None or not option_type:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO options_chain
            (symbol, expiry, strike, option_type, bid, ask, mid, iv, delta, gamma,
             theta, vega, rho, theo, bid_iv, ask_iv, contract_symbol, observed_at, source, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                normalized_symbol,
                expiry,
                strike,
                option_type,
                as_float(row.get("bid")),
                as_float(row.get("ask")),
                as_float(row.get("mid")),
                as_float(row.get("iv")),
                as_float(row.get("delta")),
                as_float(row.get("gamma")),
                as_float(row.get("theta")),
                as_float(row.get("vega")),
                as_float(row.get("rho")),
                as_float(row.get("theo")),
                as_float(row.get("bid_iv")),
                as_float(row.get("ask_iv")),
                row.get("contract_symbol") or row.get("contractSymbol") or row.get("symbol"),
                observed_at,
                source,
                json_dumps(row),
            ],
        )
        count += 1
    return count




def store_news_rows(con: Any, rows: list[dict[str, Any]], source: str) -> int:
    count = 0
    for row in rows:
        title = row.get("title")
        link = row.get("link")
        if not title:
            continue
        news_id = str(row.get("id") or stable_id(f"{source}:{title}:{link}"))
        con.execute(
            """
            INSERT OR REPLACE INTO news_items
            (id, published_at, provider, title, related_symbols, link, source, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                news_id,
                row.get("published") or datetime.utcnow().isoformat(),
                row.get("provider"),
                title,
                json_dumps(row.get("related_symbols") or []),
                link,
                source,
                json_dumps(row),
            ],
        )
        count += 1
    return count




def store_symbol_search_rows(con: Any, query: str, observed_at: str, rows: list[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        row_id = stable_id(f"tradingview-search:{query}:{observed_at}:{row.get('symbol')}:{row.get('exchange')}")
        con.execute(
            """
            INSERT OR REPLACE INTO tradingview_symbol_search
            (id, query, observed_at, symbol, description, instrument_type, exchange, country, currency, source, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row_id,
                query.upper(),
                observed_at,
                symbol,
                row.get("description"),
                row.get("type"),
                row.get("exchange"),
                row.get("country"),
                row.get("currency"),
                "tradingview",
                json_dumps(row),
            ],
        )
        count += 1
    return count




def store_watchlist_rows(con: Any, observed_at: str, rows: list[dict[str, Any]], color: str | None = None) -> int:
    count = 0
    for row in rows:
        watchlist_id = str(row.get("id") or stable_id(f"watchlist:{color}:{row.get('name')}:{row.get('symbols')}"))
        symbols = row.get("symbols") or []
        con.execute(
            """
            INSERT OR REPLACE INTO tradingview_watchlists
            (id, observed_at, name, color, symbol_count, symbols, source, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                watchlist_id,
                observed_at,
                row.get("name") or color or watchlist_id,
                row.get("color") or color,
                as_int(row.get("symbol_count")) or (len(symbols) if isinstance(symbols, list) else None),
                json_dumps(symbols),
                "tradingview",
                json_dumps(row),
            ],
        )
        count += 1
    return count




def store_alert_rows(con: Any, observed_at: str, rows: list[dict[str, Any]], alert_type: str) -> int:
    count = 0
    for row in rows:
        alert_id = str(row.get("id") or stable_id(f"alert:{alert_type}:{row.get('name')}:{row.get('symbol')}:{row.get('fired_at')}"))
        con.execute(
            """
            INSERT OR REPLACE INTO tradingview_alerts
            (id, observed_at, name, symbol, alert_type, condition, value, active, status, fired_at, source, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                alert_id,
                observed_at,
                row.get("name"),
                normalize_symbol(row.get("symbol")),
                row.get("type") or alert_type,
                row.get("condition"),
                as_float(row.get("value")),
                bool(row.get("active")) if row.get("active") is not None else None,
                row.get("status"),
                row.get("fired_at"),
                "tradingview",
                json_dumps(row),
            ],
        )
        count += 1
    return count




def store_chart_state_rows(con: Any, observed_at: str, rows: list[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        row_id = stable_id(f"chart-state:{observed_at}:{row.get('layout_id')}:{row.get('symbol')}:{row.get('url')}")
        con.execute(
            """
            INSERT OR REPLACE INTO tradingview_chart_state
            (id, observed_at, layout_id, symbol, interval, url, source, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row_id,
                observed_at,
                row.get("layout_id"),
                normalize_symbol(row.get("symbol")),
                row.get("interval"),
                row.get("url"),
                "tradingview",
                json_dumps(row),
            ],
        )
        count += 1
    return count




def store_etf_premium(con: Any, symbol: str, today: str, info: dict[str, Any]) -> bool:
    price = as_float(info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose"))
    nav = as_float(info.get("navPrice"))
    if price is None or nav is None or nav <= 0:
        return False
    premium_pct = (price - nav) / nav * 100
    con.execute(
        """
        INSERT OR REPLACE INTO etf_premiums
        (symbol, as_of, market_price, nav, premium_pct, metrics, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [symbol, today, price, nav, premium_pct, json_dumps(info), "yfinance"],
    )
    return True
