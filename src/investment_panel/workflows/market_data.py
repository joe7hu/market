"""Refresh normalized daily market facts in PostgreSQL."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from investment_panel.settings import AppConfig
from investment_panel.core.prices import fetch_prices
from investment_panel.infrastructure.postgres.monitored_universe import monitored_universe
from investment_panel.infrastructure.postgres.authority import runtime_for_config
from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.workflows.market import refresh_market_publication
from investment_panel.infrastructure.providers.yfinance_provider import YFinanceProvider


SOURCE_ID = "daily-market-prices"


def run_for_config(
    config: AppConfig,
    *,
    symbols: list[str] | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    runtime = runtime_for_config(config)
    repository = IngestionRepository(runtime)
    repository.register_source(
        SOURCE_ID,
        name="Daily market prices",
        family="market_data",
        kind="daily_bars",
        origin="Yahoo chart and Coinbase Exchange daily candles",
        capabilities={"price_bars": True, "quotes": True, "market_metrics": True},
    )
    configured_watchlist = config.watchlist
    universe_rows = monitored_universe(runtime, configured_watchlist)
    requested = {str(symbol).strip().upper() for symbol in symbols or [] if str(symbol).strip()}
    if symbols is not None:
        universe_rows = [row for row in universe_rows if row["symbol"] in requested]
    # The 200-session moving average needs more than 260 *calendar* days.
    # Always collect the explicit benchmark dependency, even when it is not a watched idea.
    if universe_rows and not any(row["symbol"] == "QQQ" for row in universe_rows):
        universe_rows.append({"symbol": "QQQ", "asset_class": "etf"})
    run_started_at = datetime.now(UTC)
    bars: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for row in universe_rows:
        symbol = row["symbol"]
        try:
            market_data = config.market_data
            lookback_days = max(market_data.lookback_days, 360 if row["asset_class"] == "crypto" else 500)
            mode = market_data.mode
            frame = fetch_prices(symbol, lookback_days, mode)
            if frame.empty:
                raise ValueError("Provider returned no price history")
            if "symbol" not in frame or set(frame["symbol"].astype(str).str.upper()) != {symbol}:
                raise ValueError("Provider returned history for the wrong instrument")
        except Exception as exc:  # each provider symbol is an independent boundary
            errors[symbol] = f"{type(exc).__name__}: {exc}"
            continue
        bars.extend(frame.to_dict("records"))
    metric_rows: list[dict[str, Any]] = []
    metric_errors: dict[str, str] = {}
    yfinance = config.data_sources.yfinance
    yfinance_enabled = yfinance.enabled
    if yfinance_enabled:
        provider = YFinanceProvider()
        observed_at = datetime.now(UTC)
        for row in universe_rows:
            symbol = row["symbol"]
            if row["asset_class"] == "crypto" or symbol.endswith("-USD"):
                continue
            try:
                metric_rows.append(_market_metrics_row(symbol, row["asset_class"], provider.market_metrics(symbol), observed_at))
            except Exception as exc:
                metric_errors[symbol] = f"{type(exc).__name__}: {exc}"
    with repository.run(SOURCE_ID, "price_bars", started_at=run_started_at) as ingestion_run:
        run_id = ingestion_run.id
        bars_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for bar in bars:
            bars_by_symbol[str(bar["symbol"]).upper()].append(bar)
        stored_by_symbol = {
            symbol: repository.store_price_bars(run_id, SOURCE_ID, symbol_bars,
                asset_classes={row["symbol"]: row["asset_class"] for row in universe_rows})
            for symbol, symbol_bars in bars_by_symbol.items()
        }
        stored = sum(stored_by_symbol.values())
        market_metrics_stored = repository.store_fundamental_observations(
            run_id,
            SOURCE_ID,
            "market_metrics",
            metric_rows,
        )
        status = ("failed" if errors and not bars else "partial" if errors or metric_errors else "succeeded")
        ingestion_run.finish(
            status,
            item_count=stored + market_metrics_stored,
            instrument_count=len(universe_rows) - len(errors),
            failure_detail="; ".join(
                f"{symbol}: {error}"
                for symbol, error in list({**errors, **metric_errors}.items())[:25]
            ) or None,
            summary={
                "requested_symbols": len(universe_rows),
                "failed_symbols": len(errors),
                "market_metrics_stored": market_metrics_stored,
                "market_metric_failures": len(metric_errors),
            },
        )
    try:
        market = (refresh_market_publication(runtime, configured_watchlist=config.watchlist)
                  if publish and symbols is None else {"status": "deferred", "reason": "scoped_refresh" if requested else "publication_disabled"})
    except Exception as exc:
        market = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    downstream_failed = market.get("status") in {"failed", "partial"}
    return {
        "status": "failed" if errors and not bars else "partial" if errors or metric_errors or downstream_failed else "ok",
        "source_status": "failed" if errors and not bars else "partial" if errors or metric_errors else "ok",
        "downstream_status": market.get("status"),
        "database": "postgresql",
        "run_id": str(run_id),
        "symbols": len(universe_rows),
        "benchmark_symbols": [row["symbol"] for row in universe_rows],
        "price_rows": stored,
        "price_rows_by_symbol": stored_by_symbol,
        "price_errors": errors,
        "market_metric_rows": market_metrics_stored,
        "market_metric_errors": metric_errors,
        "market_publication": market,
    }


def _market_metrics_row(symbol: str, asset_class: str, info: dict[str, Any], observed_at: datetime) -> dict[str, Any]:
    def number(key: str) -> float | None:
        value = info.get(key)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed == parsed else None

    market_cap = number("marketCap")
    free_cash_flow = number("freeCashflow")
    values = {
        "market_cap": market_cap,
        "shares_outstanding": number("sharesOutstanding") or number("impliedSharesOutstanding"),
        "current_price": number("currentPrice") or number("regularMarketPrice"),
        "trailing_pe": number("trailingPE"),
        "forward_pe": number("forwardPE"),
        "price_to_sales": number("priceToSalesTrailing12Months"),
        "price_to_book": number("priceToBook"),
        "total_revenue": number("totalRevenue"),
        "revenue_growth": number("revenueGrowth"),
        "profit_margin": number("profitMargins"),
        "operating_cash_flow": number("operatingCashflow"),
        "free_cash_flow": free_cash_flow,
        "fcf_yield": free_cash_flow / market_cap if free_cash_flow is not None and market_cap else None,
        "total_cash": number("totalCash"),
        "total_debt": number("totalDebt"),
        "return_on_assets": number("returnOnAssets"),
        "return_on_equity": number("returnOnEquity"),
        "return_on_invested_capital": number("returnOnInvestedCapital"),
        "target_mean_price": number("targetMeanPrice"),
        "target_median_price": number("targetMedianPrice"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "source": "yfinance_info",
    }
    return {
        "symbol": symbol,
        "name": info.get("shortName") or info.get("longName") or symbol,
        "asset_class": asset_class,
        "observed_at": observed_at,
        "period_end": observed_at.date(),
        "values": values,
    }




market_metrics_row = _market_metrics_row

__all__ = ["SOURCE_ID", "market_metrics_row", "run_for_config"]
