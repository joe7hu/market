"""Free/local source ingestion for OpenCLI, TradingView, and yfinance."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any

from investment_panel.core.config import AppConfig
from investment_panel.core.db import json_dumps, query_rows
from investment_panel.providers import OpenCliError, OpenCliRunner, TradingViewProvider
from investment_panel.providers.yfinance_provider import YFinanceProvider, YFinanceUnavailable


def update_tradingview_sources(con: Any, config: AppConfig) -> dict[str, Any]:
    if not config.data_sources.opencli.enabled or not config.data_sources.tradingview.enabled:
        return {"status": "disabled", "provider": "tradingview"}
    runner = OpenCliRunner(config.data_sources.opencli.command, config.data_sources.opencli.timeout_seconds)
    provider = TradingViewProvider(runner)
    observed_at = datetime.utcnow().isoformat()
    run_id = stable_id(f"tradingview:{observed_at}")
    result = {
        "status": "ok",
        "provider": "tradingview",
        "quotes": 0,
        "expiries": 0,
        "chains": 0,
        "screener_rows": 0,
        "news_items": 0,
    }
    try:
        status_rows = provider.status()
        record_provider_run(con, run_id, "tradingview", "status", observed_at, "ok", f"{len(status_rows)} status rows", status_rows)
        quote_symbols = equity_symbols(con)
        quote_errors = []
        for symbol in quote_symbols:
            quote = None
            for candidate in tradingview_symbol_candidates(symbol):
                try:
                    quote = provider.quote(candidate)
                except OpenCliError as exc:
                    quote_errors.append(f"{symbol}:{exc}")
                    continue
                if quote:
                    break
            if quote:
                upsert_quote(con, symbol, observed_at, quote)
                result["quotes"] += 1
        screener_rows = provider.screener(limit=config.data_sources.tradingview.screener_limit)
        store_screener_rows(con, run_id, observed_at, screener_rows)
        result["screener_rows"] = len(screener_rows)
        news_rows = provider.news(limit=config.data_sources.tradingview.news_limit)
        result["news_items"] = store_news_rows(con, news_rows, "tradingview")
        for symbol in option_symbols(con, config):
            expiries = []
            for candidate in tradingview_symbol_candidates(symbol):
                try:
                    expiries = provider.options_expiries(candidate)
                except OpenCliError:
                    continue
                if expiries:
                    break
            result["expiries"] += store_expiries(con, symbol, observed_at, expiries)
            first_expiry = next((row.get("expiry") for row in expiries if row.get("expiry")), None)
            if first_expiry:
                chain = []
                for candidate in tradingview_symbol_candidates(symbol):
                    try:
                        chain = provider.options_chain(
                            candidate,
                            str(first_expiry),
                            strikes_around_spot=config.data_sources.tradingview.strikes_around_spot,
                        )
                    except OpenCliError:
                        continue
                    if chain:
                        break
                result["chains"] += store_options_chain(con, symbol, observed_at, chain)
        if quote_errors:
            result["quote_errors"] = quote_errors[:10]
    except OpenCliError as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        record_provider_run(con, run_id, "tradingview", "refresh", observed_at, "error", str(exc), result)
        record_source_health(con, "tradingview", "error", str(exc), "opencli tradingview")
        return result
    record_provider_run(con, run_id, "tradingview", "refresh", observed_at, "ok", json_dumps(result), result)
    record_source_health(con, "tradingview", "ok", json_dumps(result), "opencli tradingview")
    return result


def update_yfinance_sources(con: Any, config: AppConfig) -> dict[str, Any]:
    if not config.data_sources.yfinance.enabled:
        return {"status": "disabled", "provider": "yfinance"}
    try:
        provider = YFinanceProvider()
    except YFinanceUnavailable as exc:
        detail = str(exc)
        record_source_health(con, "yfinance_enrichment", "missing_dependency", detail, "https://pypi.org/project/yfinance/")
        return {"status": "missing_dependency", "provider": "yfinance", "error": detail}
    today = date.today().isoformat()
    result = {"status": "ok", "provider": "yfinance", "estimates": 0, "earnings": 0, "etf_premiums": 0}
    for instrument in query_rows(con, "SELECT symbol, asset_class FROM instruments ORDER BY symbol"):
        symbol = instrument["symbol"]
        if str(symbol).endswith("-USD"):
            continue
        try:
            if instrument.get("asset_class") == "etf":
                con.execute("DELETE FROM analyst_estimates WHERE symbol = ? AND source = 'yfinance'", [symbol])
                con.execute("DELETE FROM earnings_events WHERE symbol = ? AND source = 'yfinance'", [symbol])
                info = provider.info(symbol)
                if store_etf_premium(con, symbol, today, info):
                    result["etf_premiums"] += 1
                continue
            con.execute("DELETE FROM analyst_estimates WHERE symbol = ? AND source = 'yfinance'", [symbol])
            con.execute("DELETE FROM earnings_events WHERE symbol = ? AND source = 'yfinance'", [symbol])
            estimates = provider.estimates(symbol)
            con.execute(
                """
                INSERT OR REPLACE INTO analyst_estimates (symbol, as_of, estimates, source)
                VALUES (?, ?, ?, ?)
                """,
                [symbol, today, json_dumps(estimates), "yfinance"],
            )
            result["estimates"] += 1
            events = provider.earnings_events(symbol)
            event_date = infer_event_date(events) or today
            con.execute(
                """
                INSERT OR REPLACE INTO earnings_events (symbol, event_date, event_type, metrics, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                [symbol, event_date, "earnings", json_dumps(events), "yfinance"],
            )
            result["earnings"] += 1
        except Exception as exc:
            record_source_health(con, f"yfinance:{symbol}", "error", str(exc), "https://pypi.org/project/yfinance/")
    record_source_health(con, "yfinance_enrichment", "ok", json_dumps(result), "https://pypi.org/project/yfinance/")
    return result


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
    rows = query_rows(
        con,
        """
        SELECT symbol
        FROM candidates
        WHERE symbol NOT LIKE '%-USD'
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY run_date DESC, score DESC) = 1
        ORDER BY score DESC
        LIMIT 5
        """,
    )
    if rows:
        return [row["symbol"] for row in rows]
    return equity_symbols(con)[:5]


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


def store_expiries(con: Any, symbol: str, observed_at: str, rows: list[dict[str, Any]]) -> int:
    count = 0
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
            [symbol, expiry, as_int(row.get("dte")), as_int(row.get("contracts_count")), observed_at, "tradingview", json_dumps(row)],
        )
        count += 1
    return count


def store_options_chain(con: Any, symbol: str, observed_at: str, rows: list[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        expiry = row.get("expiry")
        strike = as_float(row.get("strike"))
        option_type = row.get("type")
        if not expiry or strike is None or not option_type:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO options_chain
            (symbol, expiry, strike, option_type, bid, ask, mid, iv, delta, gamma, theta, vega, observed_at, source, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                symbol,
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
                observed_at,
                "tradingview",
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


def infer_event_date(events: dict[str, Any]) -> str | None:
    calendar = events.get("calendar")
    if isinstance(calendar, dict):
        preferred_keys = ["Earnings Date", "earningsDate", "earnings_date"]
        for key in preferred_keys:
            value = calendar.get(key)
            inferred = first_date_value(value)
            if inferred:
                return inferred
        for key, value in calendar.items():
            if "earnings" in str(key).lower() and "date" in str(key).lower():
                inferred = first_date_value(value)
                if inferred:
                    return inferred
    return None


def first_date_value(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            if item:
                return str(item)[:10]
        return None
    if value:
        return str(value)[:10]
    return None


def record_provider_run(
    con: Any,
    run_id: str,
    provider: str,
    capability: str,
    started_at: str,
    status: str,
    detail: str,
    raw: Any,
) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO provider_runs
        (id, provider, capability, started_at, finished_at, status, detail, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [run_id, provider, capability, started_at, datetime.utcnow().isoformat(), status, detail, json_dumps(raw)],
    )


def record_source_health(con: Any, source: str, status: str, detail: str, source_url: str) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO source_health
        (source, checked_at, status, detail, source_url)
        VALUES (?, ?, ?, ?, ?)
        """,
        [source, datetime.utcnow().isoformat(), status, detail, source_url],
    )


def normalize_symbol(value: Any) -> str:
    symbol = str(value or "").upper()
    return symbol.split(":")[-1]


def as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
