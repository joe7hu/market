"""Free/local source ingestion for OpenCLI, TradingView, and yfinance."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from typing import Any

from investment_panel.core.config import AppConfig
from investment_panel.core.db import json_dumps, query_rows
from investment_panel.core.decision import effective_watchlist
from investment_panel.core.options_intelligence import clear_options_intelligence, record_tradingview_options_capabilities, refresh_options_intelligence
from investment_panel.providers import OpenCliError, OpenCliRateLimitError, OpenCliRunner, TradingViewProvider
from investment_panel.providers.yfinance_provider import YFinanceProvider, YFinanceUnavailable

RADAR_MIN_DTE = 365
RADAR_MAX_DTE = 900
RADAR_MAX_EXPIRIES_PER_SYMBOL = 2
RADAR_STRIKES_AROUND_SPOT = 24
OPTION_SCAN_LIMIT = 80
# Stop the option scan after this many consecutive symbols fail with upstream
# rate limits, so a saturated limiter cannot stretch the run across the whole
# universe.
OPTION_RATE_LIMIT_CIRCUIT_BREAKER = 4


def update_tradingview_sources(con: Any, config: AppConfig, symbols: list[str] | None = None) -> dict[str, Any]:
    if not config.data_sources.opencli.enabled or not config.data_sources.tradingview.enabled:
        return {"status": "disabled", "provider": "tradingview"}
    target_symbols = unique_symbols(symbols or [])
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
        "search_rows": 0,
        "watchlists": 0,
        "alerts": 0,
        "chart_states": 0,
        "chain_expiries": 0,
        "radar_chain_expiries": 0,
    }
    if target_symbols:
        result["target_symbols"] = target_symbols
    try:
        status_rows = provider.status()
        record_provider_run(con, run_id, "tradingview", "status", observed_at, "ok", f"{len(status_rows)} status rows", status_rows)
        record_tradingview_options_capabilities(con, observed_at)
        tradingview_ready = any(row.get("connected") or row.get("app_running") for row in status_rows)
        quote_symbols = target_symbols or equity_symbols(con)
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
        if target_symbols:
            result["screener_rows"] = 0
            result["news_items"] = 0
        else:
            # Screener/news are discovery surfaces, not the radar's lifeblood.
            # Isolate their failures (notably scanner 429s) so a rate-limited
            # discovery call can never abort the option-chain ingestion below,
            # which is the only source of fresh radar snapshots.
            try:
                screener_rows = provider.screener(limit=config.data_sources.tradingview.screener_limit)
                store_screener_rows(con, run_id, observed_at, screener_rows)
                result["screener_rows"] = len(screener_rows)
            except OpenCliError as exc:
                result["screener_error"] = str(exc)
            try:
                news_rows = provider.news(limit=config.data_sources.tradingview.news_limit)
                result["news_items"] = store_news_rows(con, news_rows, "tradingview")
            except OpenCliError as exc:
                result["news_error"] = str(exc)
        if tradingview_ready:
            personal_result = update_tradingview_personal_surfaces(con, config, provider, run_id, observed_at)
            result.update(personal_result)
        else:
            result["personal_surfaces"] = "skipped_cdp_not_connected"
        requested_options_symbols = target_symbols or option_symbols(con, config)
        refreshed_option_symbols: list[str] = []
        option_errors: list[str] = []
        # Circuit breaker: if a run of consecutive symbols all fail with upstream
        # rate limits, the limiter is saturated and continuing only prolongs the
        # job (each call still pays its bounded backoff). Stop early and report a
        # partial refresh instead of dragging through the full universe.
        rate_limited_streak = 0
        for symbol in requested_options_symbols:
            if rate_limited_streak >= OPTION_RATE_LIMIT_CIRCUIT_BREAKER:
                result["options_circuit_breaker"] = (
                    f"stopped_after_{rate_limited_streak}_consecutive_rate_limited_symbols"
                )
                break
            expiries = []
            expiry_error = False
            expiry_rate_limited = False
            for candidate in tradingview_symbol_candidates(symbol):
                try:
                    expiries = provider.options_expiries(candidate)
                except OpenCliRateLimitError as exc:
                    expiry_error = True
                    expiry_rate_limited = True
                    option_errors.append(f"{symbol}:expiries:{candidate}:{exc}")
                    continue
                except OpenCliError as exc:
                    expiry_error = True
                    option_errors.append(f"{symbol}:expiries:{candidate}:{exc}")
                    continue
                if expiries:
                    break
            if expiries:
                rate_limited_streak = 0
            elif expiry_rate_limited:
                rate_limited_streak += 1
            result["expiries"] += store_expiries(con, symbol, observed_at, expiries)
            selected_expiries = selected_option_expiries(expiries, observed_at)
            if selected_expiries:
                symbol_chain_rows = 0
                any_chain_error = False
                for expiry in selected_expiries:
                    strikes_around_spot = option_chain_strikes_around_spot(
                        expiry,
                        expiries,
                        observed_at,
                        configured=config.data_sources.tradingview.strikes_around_spot,
                    )
                    chain = []
                    chain_error = False
                    for candidate in tradingview_symbol_candidates(symbol):
                        try:
                            chain = provider.options_chain(
                                candidate,
                                str(expiry),
                                strikes_around_spot=strikes_around_spot,
                            )
                        except OpenCliError as exc:
                            chain_error = True
                            option_errors.append(f"{symbol}:chain:{candidate}:{exc}")
                            continue
                        if chain:
                            break
                    stored_chain_rows = store_options_chain(con, symbol, observed_at, chain)
                    result["chains"] += stored_chain_rows
                    if stored_chain_rows:
                        symbol_chain_rows += stored_chain_rows
                        result["chain_expiries"] += 1
                        if strikes_around_spot > config.data_sources.tradingview.strikes_around_spot:
                            result["radar_chain_expiries"] += 1
                    if chain_error:
                        any_chain_error = True
                if symbol_chain_rows:
                    refreshed_option_symbols.append(symbol)
                elif not any_chain_error:
                    clear_options_intelligence(con, [symbol], source="tradingview")
            elif not expiry_error:
                clear_options_intelligence(con, [symbol], source="tradingview")
        if refreshed_option_symbols:
            result["options_intelligence"] = refresh_options_intelligence(con, refreshed_option_symbols, source="tradingview")
        if option_errors:
            result["option_errors"] = option_errors[:25]
            result["option_error_count"] = len(option_errors)
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


def update_tradingview_personal_surfaces(
    con: Any,
    config: AppConfig,
    provider: TradingViewProvider,
    run_id: str,
    observed_at: str,
) -> dict[str, Any]:
    """Refresh read-only TradingView surfaces that require the desktop session."""

    result: dict[str, Any] = {"personal_surfaces": "ok", "search_rows": 0, "watchlists": 0, "alerts": 0, "chart_states": 0}
    errors: list[str] = []

    def record_error(capability: str, exc: OpenCliError) -> None:
        detail = str(exc)
        errors.append(f"{capability}:{detail}")
        record_provider_run(
            con,
            stable_id(f"{run_id}:{capability}:error"),
            "tradingview",
            capability,
            observed_at,
            "error",
            detail,
            {"error": detail},
        )

    if config.data_sources.tradingview.chart_state_enabled:
        try:
            chart_rows = provider.chart_state()
            result["chart_states"] = store_chart_state_rows(con, observed_at, chart_rows)
            record_provider_run(
                con,
                stable_id(f"{run_id}:chart-state"),
                "tradingview",
                "chart-state",
                observed_at,
                "ok",
                f"{result['chart_states']} chart-state rows",
                chart_rows,
            )
        except OpenCliError as exc:
            record_error("chart-state", exc)

    search_symbols = tradingview_search_symbols(con, config)
    for symbol in search_symbols:
        try:
            search_rows = provider.search(symbol, limit=5)
            result["search_rows"] += store_symbol_search_rows(con, symbol, observed_at, search_rows)
        except OpenCliError as exc:
            record_error(f"search:{symbol}", exc)
    if search_symbols:
        record_provider_run(
            con,
            stable_id(f"{run_id}:search"),
            "tradingview",
            "search",
            observed_at,
            "ok" if not any(error.startswith("search:") for error in errors) else "partial",
            f"{result['search_rows']} search rows across {len(search_symbols)} symbols",
            {"symbols": search_symbols, "rows": result["search_rows"]},
        )

    if config.data_sources.tradingview.personal_surfaces_enabled:
        try:
            watchlist_rows = provider.watchlists()
            result["watchlists"] += store_watchlist_rows(con, observed_at, watchlist_rows)
            for color in config.data_sources.tradingview.watchlist_colors:
                color_rows = provider.watchlists(color=color)
                result["watchlists"] += store_watchlist_rows(con, observed_at, color_rows, color=color)
            record_provider_run(
                con,
                stable_id(f"{run_id}:watchlists"),
                "tradingview",
                "watchlists",
                observed_at,
                "ok",
                f"{result['watchlists']} watchlist rows",
                {"rows": result["watchlists"]},
            )
        except OpenCliError as exc:
            record_error("watchlists", exc)

        alert_rows_total = 0
        for alert_type in config.data_sources.tradingview.alert_types:
            try:
                alert_rows = provider.alerts(alert_type)
                alert_rows_total += store_alert_rows(con, observed_at, alert_rows, alert_type)
            except OpenCliError as exc:
                record_error(f"alerts:{alert_type}", exc)
        result["alerts"] = alert_rows_total
        if config.data_sources.tradingview.alert_types:
            record_provider_run(
                con,
                stable_id(f"{run_id}:alerts"),
                "tradingview",
                "alerts",
                observed_at,
                "ok" if not any(error.startswith("alerts:") for error in errors) else "partial",
                f"{alert_rows_total} alert rows",
                {"types": config.data_sources.tradingview.alert_types, "rows": alert_rows_total},
            )

    if errors:
        result["personal_surfaces"] = "partial"
        result["personal_errors"] = errors[:10]
        record_source_health(con, "tradingview_personal", "warning", json_dumps(result), "opencli tradingview")
    else:
        record_source_health(con, "tradingview_personal", "ok", json_dumps(result), "opencli tradingview")
    return result


def update_yfinance_sources(con: Any, config: AppConfig, symbols: list[str] | None = None) -> dict[str, Any]:
    if not config.data_sources.yfinance.enabled:
        return {"status": "disabled", "provider": "yfinance"}
    try:
        provider = YFinanceProvider()
    except YFinanceUnavailable as exc:
        detail = str(exc)
        record_source_health(con, "yfinance_enrichment", "missing_dependency", detail, "https://pypi.org/project/yfinance/")
        return {"status": "missing_dependency", "provider": "yfinance", "error": detail}
    today = date.today().isoformat()
    observed_at = datetime.utcnow().isoformat()
    run_id = stable_id(f"yfinance:{observed_at}")
    target_symbols = unique_symbols(symbols or [])
    result = {
        "status": "ok",
        "provider": "yfinance",
        "estimates": 0,
        "earnings": 0,
        "etf_premiums": 0,
        "market_snapshots": 0,
        "options_liquidity": 0,
        "options_liquidity_expiries": 0,
    }
    if target_symbols:
        result["target_symbols"] = target_symbols
    for instrument in yfinance_instruments(con, target_symbols):
        symbol = instrument["symbol"]
        if str(symbol).endswith("-USD"):
            continue
        try:
            info = provider.info(symbol)
            update_instrument_from_yfinance(con, symbol, info)
            if store_yfinance_market_snapshot(con, run_id, symbol, observed_at, info):
                result["market_snapshots"] += 1
            if instrument.get("asset_class") == "etf":
                con.execute("DELETE FROM analyst_estimates WHERE symbol = ? AND source = 'yfinance'", [symbol])
                con.execute("DELETE FROM earnings_events WHERE symbol = ? AND source = 'yfinance'", [symbol])
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
    chain_result = update_yfinance_options_chains(con, provider, target_symbols or option_symbols(con, config), observed_at, run_id, config)
    result.update(chain_result)
    liquidity_result = update_yfinance_options_liquidity(con, provider, target_symbols, observed_at, run_id)
    result.update(liquidity_result)
    record_source_health(con, "yfinance_enrichment", "ok", json_dumps(result), "https://pypi.org/project/yfinance/")
    return result


def update_yfinance_options_chains(
    con: Any,
    provider: YFinanceProvider,
    symbols: list[str] | None,
    observed_at: str,
    run_id: str,
    config: AppConfig,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "options_expiries": 0,
        "options_chains": 0,
        "options_chain_expiries": 0,
        "options_chain_symbols": 0,
        "options_chain_symbols_requested": 0,
    }
    errors: list[str] = []
    record_yfinance_options_chain_capabilities(con, observed_at)
    requested_symbols = unique_symbols(symbols or [])
    result["options_chain_symbols_requested"] = len(requested_symbols)
    for symbol in requested_symbols:
        try:
            expiries = provider.options_expiries(symbol)
        except Exception as exc:
            errors.append(f"{symbol}:expiries:{exc}")
            continue
        result["options_expiries"] += store_expiries(con, symbol, observed_at, expiries, source="yfinance")
        selected_expiries = selected_option_expiries(expiries, observed_at)
        if not selected_expiries:
            continue
        spot = latest_option_scan_spot(con, symbol)
        symbol_chain_rows = 0
        for expiry in selected_expiries:
            try:
                chain = provider.options_chain(symbol, str(expiry))
            except Exception as exc:
                errors.append(f"{symbol}:chain:{expiry}:{exc}")
                continue
            strikes_around_spot = option_chain_strikes_around_spot(
                str(expiry),
                expiries,
                observed_at,
                configured=config.data_sources.tradingview.strikes_around_spot,
            )
            filtered_chain = filter_chain_rows_around_spot(chain, spot, strikes_around_spot)
            stored = store_options_chain(con, symbol, observed_at, filtered_chain, source="yfinance")
            result["options_chains"] += stored
            if stored:
                result["options_chain_expiries"] += 1
                symbol_chain_rows += stored
        if symbol_chain_rows:
            result["options_chain_symbols"] += 1
    status = "ok" if not errors else "partial"
    detail = json_dumps(result if not errors else {**result, "errors": errors[:10], "error_count": len(errors)})
    record_provider_run(con, stable_id(f"{run_id}:options-chains"), "yfinance", "options-chains", observed_at, status, detail, result)
    record_source_health(con, "yfinance_options_chains", status, detail, "https://pypi.org/project/yfinance/")
    if errors:
        result["options_chain_errors"] = errors[:10]
        result["options_chain_error_count"] = len(errors)
    return result


def record_yfinance_options_chain_capabilities(con: Any, observed_at: str) -> None:
    raw = {
        "available_fields": ["expiry", "strike", "type", "bid", "ask", "mid", "last", "iv", "volume", "open_interest", "contract_symbol"],
        "role": "primary_option_chain_source",
    }
    con.execute(
        """
        INSERT OR REPLACE INTO options_provider_capabilities
        (provider, observed_at, supports_expiries, supports_chain_quotes,
         supports_greeks, supports_theoretical_price, supports_open_interest,
         supports_volume, supports_full_chain, status, detail, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            "yfinance_options_chains",
            observed_at,
            True,
            True,
            False,
            False,
            True,
            True,
            True,
            "primary",
            "Yahoo/yfinance supplies expiries and full option chain bid/ask/last/IV/OI/volume for broad deterministic 10x radar coverage; Greeks are unavailable.",
            json_dumps(raw),
        ],
    )


def yfinance_instruments(con: Any, symbols: list[str] | None = None) -> list[dict[str, Any]]:
    target_symbols = unique_symbols(symbols or [])
    if not target_symbols:
        return query_rows(con, "SELECT symbol, asset_class FROM instruments ORDER BY symbol")
    placeholders = ", ".join(["?"] * len(target_symbols))
    existing = query_rows(con, f"SELECT symbol, asset_class FROM instruments WHERE symbol IN ({placeholders}) ORDER BY symbol", target_symbols)
    found = {str(row["symbol"]).upper() for row in existing}
    missing = [{"symbol": symbol, "asset_class": "equity"} for symbol in target_symbols if symbol not in found]
    return [*existing, *missing]


def update_yfinance_options_liquidity(
    con: Any,
    provider: YFinanceProvider,
    symbols: list[str] | None,
    observed_at: str,
    run_id: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {"options_liquidity": 0, "options_liquidity_expiries": 0}
    errors: list[str] = []
    record_yfinance_options_liquidity_capabilities(con, observed_at)
    for chain in latest_tradingview_option_chain_expiries(con, symbols):
        symbol = str(chain["symbol"]).upper()
        expiry = str(chain["expiry"])
        try:
            liquidity_rows = provider.options_chain_liquidity(symbol, expiry)
        except Exception as exc:
            errors.append(f"{symbol}:{expiry}:{exc}")
            continue
        updated = store_yfinance_options_liquidity(
            con,
            symbol,
            expiry,
            observed_at,
            chain["observed_at"],
            liquidity_rows,
        )
        if updated:
            result["options_liquidity"] += updated
            result["options_liquidity_expiries"] += 1
    status = "ok" if not errors else "partial"
    detail = json_dumps(result if not errors else {**result, "errors": errors[:10], "error_count": len(errors)})
    record_provider_run(con, stable_id(f"{run_id}:options-liquidity"), "yfinance", "options-liquidity", observed_at, status, detail, result)
    record_source_health(con, "yfinance_options_liquidity", status, detail, "https://pypi.org/project/yfinance/")
    if errors:
        result["options_liquidity_errors"] = errors[:10]
        result["options_liquidity_error_count"] = len(errors)
    return result


def record_yfinance_options_liquidity_capabilities(con: Any, observed_at: str) -> None:
    raw = {
        "available_fields": ["expiry", "strike", "type", "volume", "open_interest", "openInterest", "last", "contract_symbol"],
        "role": "supplemental_liquidity_overlay",
        "base_chain_source": "tradingview",
    }
    con.execute(
        """
        INSERT OR REPLACE INTO options_provider_capabilities
        (provider, observed_at, supports_expiries, supports_chain_quotes,
         supports_greeks, supports_theoretical_price, supports_open_interest,
         supports_volume, supports_full_chain, status, detail, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            "yfinance_options_liquidity",
            observed_at,
            True,
            False,
            False,
            False,
            True,
            True,
            True,
            "supplemental",
            "Yahoo/yfinance supplies option contract open interest and volume, merged into TradingView quote/Greek chain rows for deterministic liquidity gates.",
            json_dumps(raw),
        ],
    )


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
        [scan_limit],
    )
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
        [scan_limit],
    )
    selected = unique_symbols(
        [
            *watchlist,
            *[row["symbol"] for row in decision_rows],
            *[row["symbol"] for row in candidate_rows],
            *equity_symbols(con),
        ]
    )
    return selected[: max(scan_limit, len(watchlist))]


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


def _radar_expiry_targets(min_dte: int, max_dte: int, count: int) -> list[int]:
    if count <= 1:
        return [min_dte]
    step = (max_dte - min_dte) / max(1, count - 1)
    return [round(min_dte + step * index) for index in range(count)]


def _dte_from_expiry(expiry: str, observed_at: str) -> int | None:
    try:
        expiry_date = date.fromisoformat(expiry[:10])
        observed_date = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None
    return (expiry_date - observed_date).days


def _unique_strings(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


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


def unique_symbols(symbols: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        normalized = normalize_symbol(symbol)
        if not normalized or normalized in seen or normalized.endswith("-USD"):
            continue
        seen.add(normalized)
        output.append(normalized)
    return output


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
    for grouped_rows in grouped.values():
        with_strikes = [(as_float(row.get("strike")), row) for row in grouped_rows]
        usable = [(strike, row) for strike, row in with_strikes if strike is not None]
        if not usable:
            filtered.extend(grouped_rows)
            continue
        nearest = sorted(usable, key=lambda item: abs(float(item[0]) - spot))[:max_rows_per_type]
        filtered.extend(row for _strike, row in sorted(nearest, key=lambda item: float(item[0])))
    return filtered


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


def parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def normalize_symbol(value: Any) -> str:
    symbol = str(value or "").upper()
    return symbol.split(":")[-1]


def as_float(value: Any) -> float | None:
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if number is None or not math.isfinite(number):
        return None
    return number


def as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
