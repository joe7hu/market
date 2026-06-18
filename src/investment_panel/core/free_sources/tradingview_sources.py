"""TradingView source and personal-surface updates."""

from __future__ import annotations
from datetime import date, datetime
from typing import Any
from investment_panel.core.config import AppConfig
from investment_panel.core.db import json_dumps, query_rows
from investment_panel.core.options_intelligence import clear_options_intelligence, record_tradingview_options_capabilities, refresh_options_intelligence
from investment_panel.providers import OpenCliError, OpenCliRateLimitError, OpenCliRunner, TradingViewProvider

from investment_panel.core.free_sources.constants import OPTION_RATE_LIMIT_CIRCUIT_BREAKER
from investment_panel.core.free_sources.coerce import stable_id, unique_symbols
from investment_panel.core.free_sources.provenance import record_provider_run, record_source_health
from investment_panel.core.free_sources.options import equity_symbols, filter_chain_rows_around_spot, latest_option_scan_spot, option_chain_strikes_around_spot, option_symbols, selected_option_expiries, tradingview_search_symbols, tradingview_symbol_candidates
from investment_panel.core.free_sources.store import store_alert_rows, store_chart_state_rows, store_expiries, store_news_rows, store_options_chain, store_screener_rows, store_symbol_search_rows, store_watchlist_rows, upsert_quote



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
            result.update(_update_tradingview_search(con, provider, run_id, observed_at, target_symbols))
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
            search_rows_before_personal = int(result.get("search_rows", 0) or 0)
            personal_result = update_tradingview_personal_surfaces(
                con,
                config,
                provider,
                run_id,
                observed_at,
                search_symbols=[] if target_symbols else None,
            )
            result.update(personal_result)
            result["search_rows"] = search_rows_before_personal + int(personal_result.get("search_rows", 0) or 0)
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
                spot = latest_option_scan_spot(con, symbol)
                symbol_chain_rows = 0
                any_chain_error = False
                for expiry in selected_expiries:
                    strikes_around_spot = option_chain_strikes_around_spot(
                        expiry,
                        expiries,
                        observed_at,
                        configured=config.data_sources.tradingview.strikes_around_spot,
                    )
                    fetch_strikes_around_spot = strikes_around_spot
                    if strikes_around_spot > config.data_sources.tradingview.strikes_around_spot:
                        fetch_strikes_around_spot = strikes_around_spot * 2
                    chain = []
                    chain_error = False
                    for candidate in tradingview_symbol_candidates(symbol):
                        try:
                            chain = provider.options_chain(
                                candidate,
                                str(expiry),
                                strikes_around_spot=fetch_strikes_around_spot,
                            )
                        except OpenCliError as exc:
                            chain_error = True
                            option_errors.append(f"{symbol}:chain:{candidate}:{exc}")
                            continue
                        if chain:
                            break
                    chain = filter_chain_rows_around_spot(chain, spot, strikes_around_spot)
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


def _update_tradingview_search(
    con: Any,
    provider: TradingViewProvider,
    run_id: str,
    observed_at: str,
    symbols: list[str],
) -> dict[str, Any]:
    search_targets = unique_symbols(symbols)
    result: dict[str, Any] = {"search_rows": 0}
    errors: list[str] = []
    for symbol in search_targets:
        try:
            search_rows = provider.search(symbol, limit=5)
            result["search_rows"] += store_symbol_search_rows(con, symbol, observed_at, search_rows)
        except OpenCliError as exc:
            errors.append(f"search:{symbol}:{exc}")
    if search_targets:
        status = "ok" if not errors else "partial"
        record_provider_run(
            con,
            stable_id(f"{run_id}:search"),
            "tradingview",
            "search",
            observed_at,
            status,
            f"{result['search_rows']} search rows across {len(search_targets)} symbols",
            {"symbols": search_targets, "rows": result["search_rows"], "errors": errors[:10]},
        )
    if errors:
        result["search_errors"] = errors[:10]
        result["search_error_count"] = len(errors)
    return result




def update_tradingview_personal_surfaces(
    con: Any,
    config: AppConfig,
    provider: TradingViewProvider,
    run_id: str,
    observed_at: str,
    search_symbols: list[str] | None = None,
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

    search_targets = tradingview_search_symbols(con, config) if search_symbols is None else search_symbols
    search_result = _update_tradingview_search(con, provider, run_id, observed_at, search_targets)
    result["search_rows"] = search_result["search_rows"]
    for error in search_result.get("search_errors", []):
        errors.append(error)

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
