"""Refresh normalized daily market facts in PostgreSQL."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.prices import fetch_prices
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.market_analysis import refresh_market_publication
from investment_panel.providers.yfinance_provider import YFinanceProvider


SOURCE_ID = "daily-market-prices"


def run(
    config_path: str | None = None,
    *,
    symbols: list[str] | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    config = load_config(config_path)
    runtime = runtime_for_config(config)
    repository = IngestionRepository(runtime)
    repository.register_source(
        SOURCE_ID,
        name="Daily market prices",
        family="market_data",
        kind="daily_bars",
        origin="Yahoo chart and CoinGecko",
        capabilities={"price_bars": True, "quotes": True, "market_metrics": True},
    )
    universe_rows = _universe(runtime, config.watchlist)
    requested = {str(symbol).strip().upper() for symbol in symbols or [] if str(symbol).strip()}
    if requested:
        universe_rows = [row for row in universe_rows if row["symbol"] in requested]
    run_id = repository.start_run(SOURCE_ID, "price_bars")
    bars: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for row in universe_rows:
        symbol = row["symbol"]
        try:
            frame = fetch_prices(symbol, config.market_data.lookback_days, config.market_data.mode)
        except Exception as exc:  # each provider symbol is an independent boundary
            errors[symbol] = f"{type(exc).__name__}: {exc}"
            continue
        bars.extend(frame.to_dict("records"))
    metric_rows: list[dict[str, Any]] = []
    metric_errors: dict[str, str] = {}
    if config.data_sources.yfinance.enabled:
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
    try:
        stored = repository.store_price_bars(
            run_id,
            SOURCE_ID,
            bars,
            asset_classes={row["symbol"]: row["asset_class"] for row in universe_rows},
        )
        market_metrics_stored = repository.store_fundamental_observations(
            run_id,
            SOURCE_ID,
            "market_metrics",
            metric_rows,
        )
        status = "partial" if errors or metric_errors else "succeeded"
        repository.finish_run(
            run_id,
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
    except Exception as exc:
        repository.finish_run(run_id, "failed", failure_detail=f"{type(exc).__name__}: {exc}")
        raise
    market = refresh_market_publication(runtime) if publish else {"status": "deferred"}
    return {
        "status": "partial" if errors or metric_errors else "ok",
        "database": "postgresql",
        "run_id": str(run_id),
        "symbols": len(universe_rows),
        "price_rows": stored,
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


def _universe(runtime: Any, configured: list[dict[str, Any]]) -> list[dict[str, str]]:
    with runtime.read() as connection:
        rows = connection.execute(
            """
            SELECT instrument.symbol, instrument.asset_class,
                   position.instrument_id IS NOT NULL AS is_owned, watchlist.watch_state
            FROM catalog.instrument instrument
            LEFT JOIN app.portfolio_position position ON position.instrument_id = instrument.id
            LEFT JOIN app.watchlist_item watchlist ON watchlist.instrument_id = instrument.id
            WHERE position.instrument_id IS NOT NULL OR watchlist.instrument_id IS NOT NULL
            ORDER BY (position.instrument_id IS NOT NULL) DESC, instrument.symbol
            """
        ).fetchall()
    output: dict[str, dict[str, str]] = {}
    for row in rows:
        if row["watch_state"] == "excluded" and not row["is_owned"]:
            continue
        output[str(row["symbol"])] = {
            "symbol": str(row["symbol"]),
            "asset_class": str(row["asset_class"] or "equity"),
        }
    for item in configured:
        symbol = str(item.get("symbol") or "").strip().upper()
        if symbol and symbol not in output and str(item.get("watch_state") or "") != "excluded":
            output[symbol] = {"symbol": symbol, "asset_class": str(item.get("asset_class") or "equity")}
    return list(output.values())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--symbol", action="append", dest="symbols", default=None)
    args = parser.parse_args()
    print(json.dumps(run(args.config, symbols=args.symbols), indent=2, default=str))


if __name__ == "__main__":
    main()
