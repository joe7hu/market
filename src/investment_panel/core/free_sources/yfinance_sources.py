"""yfinance equity, options-chain, and liquidity updates."""

from __future__ import annotations
import time
from datetime import date, datetime
from typing import Any
from investment_panel.core.config import AppConfig
from investment_panel.core.db import json_dumps, query_rows
from investment_panel.providers.yfinance_provider import YFinanceProvider, YFinanceUnavailable

from investment_panel.core.free_sources.constants import OPTION_RATE_LIMIT_CIRCUIT_BREAKER, YFINANCE_OPTION_THROTTLE_SECONDS
from investment_panel.core.free_sources.coerce import _is_rate_limit_error, infer_event_date, stable_id, unique_symbols
from investment_panel.core.free_sources.provenance import record_provider_run, record_source_health
from investment_panel.core.free_sources.options import filter_chain_rows_around_spot, latest_option_scan_spot, latest_tradingview_option_chain_expiries, option_chain_strikes_around_spot, option_symbols, selected_option_expiries
from investment_panel.core.free_sources.store import store_etf_premium, store_expiries, store_options_chain, store_yfinance_market_snapshot, store_yfinance_options_liquidity, update_instrument_from_yfinance



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
    # Scope liquidity enrichment to the radar option universe, matching the chain
    # job above. Unscoped, it swept the entire TradingView chain table (~3x the
    # radar universe, incl. non-radar symbols and expired expiries), saturating
    # the yfinance rate limiter so every call 429'd and zero OI/volume landed.
    liquidity_result = update_yfinance_options_liquidity(con, provider, target_symbols or option_symbols(con, config), observed_at, run_id)
    result.update(liquidity_result)
    # Report the real health, not a hardcoded "ok": a run that 429'd every option
    # call or tripped a circuit breaker must not show green in source_health.
    result["status"] = _yfinance_enrichment_status(result)
    record_source_health(con, "yfinance_enrichment", result["status"], json_dumps(result), "https://pypi.org/project/yfinance/")
    return result




def _yfinance_enrichment_status(result: dict[str, Any]) -> str:
    """Derive enrichment health from what the sub-jobs actually did.

    'ok' only when nothing errored and no circuit breaker tripped; 'error' when
    errors occurred and nothing was produced; otherwise 'partial'.
    """

    error_count = int(result.get("options_chain_error_count", 0) or 0) + int(result.get("options_liquidity_error_count", 0) or 0)
    circuit_broken = bool(result.get("options_chain_circuit_breaker") or result.get("options_liquidity_circuit_breaker"))
    produced = sum(
        int(result.get(key, 0) or 0)
        for key in ("options_chains", "options_liquidity", "market_snapshots", "estimates", "earnings", "etf_premiums")
    )
    if not error_count and not circuit_broken:
        return "ok"
    return "error" if not produced else "partial"




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
    # Shares Yahoo's per-IP limiter with the liquidity job, so it carries the same
    # throttle + rate-limit circuit breaker: spacing keeps the combined burst under
    # the limit, and the breaker stops once saturated instead of grinding the whole
    # universe and deepening the throttle (which would also starve the liquidity job).
    rate_limited_streak = 0
    for symbol in requested_symbols:
        if rate_limited_streak >= OPTION_RATE_LIMIT_CIRCUIT_BREAKER:
            result["options_chain_circuit_breaker"] = f"stopped_after_{rate_limited_streak}_consecutive_rate_limited_calls"
            break
        try:
            expiries = provider.options_expiries(symbol)
        except Exception as exc:
            errors.append(f"{symbol}:expiries:{exc}")
            if _is_rate_limit_error(exc):
                rate_limited_streak += 1
            continue
        rate_limited_streak = 0
        if YFINANCE_OPTION_THROTTLE_SECONDS:
            time.sleep(YFINANCE_OPTION_THROTTLE_SECONDS)
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
                if _is_rate_limit_error(exc):
                    rate_limited_streak += 1
                continue
            rate_limited_streak = 0
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
            if YFINANCE_OPTION_THROTTLE_SECONDS:
                time.sleep(YFINANCE_OPTION_THROTTLE_SECONDS)
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
    today = date.today().isoformat()
    rate_limited_streak = 0
    for chain in latest_tradingview_option_chain_expiries(con, symbols):
        symbol = str(chain["symbol"]).upper()
        expiry = str(chain["expiry"])
        # Skip already-expired expiries: they carry no live OI/volume and only
        # burn rate-limit budget against the live contracts we actually need.
        if expiry < today:
            continue
        # Circuit breaker: once the limiter is saturated, every further call 429s
        # and only deepens the throttle. Stop and report partial so the limiter
        # can cool before the next run, instead of grinding the whole universe.
        if rate_limited_streak >= OPTION_RATE_LIMIT_CIRCUIT_BREAKER:
            result["options_liquidity_circuit_breaker"] = f"stopped_after_{rate_limited_streak}_consecutive_rate_limited_calls"
            break
        try:
            liquidity_rows = provider.options_chain_liquidity(symbol, expiry)
        except Exception as exc:
            errors.append(f"{symbol}:{expiry}:{exc}")
            if _is_rate_limit_error(exc):
                rate_limited_streak += 1
            continue
        rate_limited_streak = 0
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
        if YFINANCE_OPTION_THROTTLE_SECONDS:
            time.sleep(YFINANCE_OPTION_THROTTLE_SECONDS)
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
