"""Small, causal daily-bar repairs for crypto rollover and recovery references.

Use the existing daily source/fact identity: retries cannot duplicate candles.
Dependency checks do not stand in for a full daily-source health check. Provider
I/O happens outside transactions, in four bounded workers; only exact required
completed dates are written, never quotes or synthetic history.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from typing import Any

from investment_panel.core.market_time import market_timezone_for_symbol
from investment_panel.core.prices import fetch_prices
from investment_panel.infrastructure.postgres.confirmed_daily_prices import (
    completed_trading_dates,
    confirmed_daily_bars,
)
from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.option_events import OptionEventRepository
from investment_panel.infrastructure.postgres.recovery_universe import (
    detector_symbol_limit,
    detector_universe,
)
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.settings import AppConfig

SOURCE_ID = "daily-market-prices"
CAPABILITY = "price_bar_dependencies"


def register_daily_source(repository: IngestionRepository) -> None:
    repository.register_source(
        SOURCE_ID,
        name="Daily market prices",
        family="market_data",
        kind="daily_bars",
        origin="Yahoo chart and Coinbase Exchange daily candles",
        capabilities={"price_bars": True, "quotes": True, "market_metrics": True},
    )


def recovery_reference_universe(
    runtime: DatabaseRuntime, config: AppConfig
) -> list[dict[str, str]]:
    provider = config.data_sources.brokers.robinhood
    if not provider.enabled:
        return []
    symbols, _ = detector_universe(
        IngestionRepository(runtime),
        OptionEventRepository(runtime),
        configured=config.watchlist,
        limit=detector_symbol_limit(provider.max_symbols),
    )
    if not symbols:
        return []
    with runtime.read() as connection:
        assets = {
            row["symbol"]: row["asset_class"]
            for row in connection.execute(
                "SELECT symbol, asset_class FROM catalog.instrument WHERE symbol = ANY(%s)",
                [symbols],
            ).fetchall()
        }
    configured = {
        str(row.get("symbol", "")).upper(): row.get("asset_class", "equity")
        for row in config.watchlist
    }
    return [
        {
            "symbol": symbol,
            "asset_class": str(assets.get(symbol, configured.get(symbol, "equity"))),
        }
        for symbol in symbols
    ]


def required_dates(
    universe: list[dict[str, Any]], now: datetime, *, count: int = 1
) -> dict[str, list[date]]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("dependency cutoff must be timezone-aware")
    if isinstance(count, bool) or count not in (1, 2, 3):
        raise ValueError("dependency repair is limited to one to three completed bars")
    reference = now.astimezone(UTC)
    result: dict[str, list[date]] = {}
    for row in universe:
        symbol = str(row["symbol"]).strip().upper()
        asset = row.get("asset_class", "equity")
        if asset == "crypto":
            result[symbol] = [
                reference.date() - timedelta(days=offset)
                for offset in range(1, count + 1)
            ]
        elif (
            asset in {"equity", "etf"}
            and market_timezone_for_symbol(symbol) == "America/New_York"
        ):
            result[symbol] = list(completed_trading_dates(reference, count=count))
    return result


def missing_dates(
    runtime: DatabaseRuntime,
    universe: list[dict[str, Any]],
    *,
    now: datetime,
    count: int = 1,
) -> dict[str, list[date]]:
    expected = required_dates(universe, now, count=count)
    if not expected:
        return {}
    assets = {
        str(row["symbol"]).upper(): row.get("asset_class", "equity") for row in universe
    }
    with runtime.read() as connection:
        ids = {
            row["symbol"]: int(row["id"])
            for row in connection.execute(
                "SELECT id, symbol FROM catalog.instrument WHERE symbol = ANY(%s)",
                [list(expected)],
            ).fetchall()
        }
        missing: dict[str, list[date]] = {}
        # Two set-based reads, not one query or full-history scan per symbol.
        for crypto in (False, True):
            symbols = [
                symbol for symbol in expected if (assets[symbol] == "crypto") == crypto
            ]
            dates = sorted({day for symbol in symbols for day in expected[symbol]})
            bars = (
                confirmed_daily_bars(
                    connection,
                    [ids[symbol] for symbol in symbols if symbol in ids],
                    as_of=now,
                    trading_dates=dates,
                    require_session_close=not crypto,
                )
                if dates
                else {}
            )
            for symbol in symbols:
                available = {
                    bar["trading_date"] for bar in bars.get(ids.get(symbol, -1), [])
                }
                absent = [day for day in expected[symbol] if day not in available]
                if absent:
                    missing[symbol] = absent
    return missing


def _candles(symbol: str, dates: list[date], mode: str) -> list[dict[str, Any]]:
    frame = fetch_prices(symbol, 30, mode)
    if (
        frame.empty
        or "symbol" not in frame
        or set(frame["symbol"].astype(str).str.upper()) != {symbol}
    ):
        raise ValueError("Provider returned no matching instrument candles")
    accepted = []
    for row in frame.to_dict("records"):
        day = date.fromisoformat(str(row.get("date"))[:10])
        if day not in dates:
            continue
        prices = [float(row.get(key)) for key in ("open", "high", "low", "close")]
        volume = float(row.get("volume"))
        if (
            not all(isfinite(value) and value > 0 for value in prices)
            or not isfinite(volume)
            or volume < 0
        ):
            raise ValueError("Provider returned invalid OHLCV")
        opening, high, low, close = prices
        if (
            low > min(opening, close)
            or high < max(opening, close)
            or row.get("is_complete") is not True
        ):
            raise ValueError("Provider returned an incomplete or inconsistent candle")
        accepted.append({**row, "symbol": symbol, "date": day})
    return accepted


def refresh_dependencies(
    runtime: DatabaseRuntime,
    config: AppConfig,
    universe: list[dict[str, Any]],
    *,
    count: int = 1,
) -> dict[str, Any]:
    reference = datetime.now(UTC)
    missing = missing_dates(runtime, universe, now=reference, count=count)
    if not missing:
        return {
            "status": "ok",
            "stored": 0,
            "requested": 0,
            "missing_symbols": [],
            "errors": {},
        }
    rows: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(_candles, symbol, dates, config.market_data.mode): symbol
            for symbol, dates in missing.items()
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                rows.extend(future.result())
            except Exception as exc:
                errors[symbol] = f"{type(exc).__name__}: {exc}"
    repository = IngestionRepository(runtime)
    register_daily_source(repository)
    assets = {
        str(row["symbol"]).upper(): str(row.get("asset_class", "equity"))
        for row in universe
    }
    with repository.run(SOURCE_ID, CAPABILITY, started_at=reference) as ingestion:
        stored = repository.store_price_bars(
            ingestion.id, SOURCE_ID, rows, asset_classes=assets
        )
        received = {(row["symbol"], row["date"]) for row in rows}
        unreturned = sorted(
            symbol
            for symbol, dates in missing.items()
            if any((symbol, day) not in received for day in dates)
        )
        ingestion.finish(
            "partial" if errors or unreturned else "succeeded",
            item_count=stored,
            instrument_count=len(missing) - len(unreturned),
            failure_detail="; ".join(
                [
                    *(f"{symbol}: {reason}" for symbol, reason in errors.items()),
                    *(
                        f"{symbol}: required completed candle missing"
                        for symbol in unreturned
                    ),
                ]
            )[:4000]
            or None,
            summary={
                "scope": "completed_bar_dependencies",
                "requested_symbols": sorted(missing),
                "missing_symbols": unreturned,
                "required_dates": {
                    symbol: [day.isoformat() for day in dates]
                    for symbol, dates in missing.items()
                },
            },
        )
    # The producer's real completion clock is the new evidence cutoff. Never
    # use the pre-fetch cutoff or assign availability to the historical date.
    remaining = missing_dates(runtime, universe, now=datetime.now(UTC), count=count)
    return {
        "status": "partial" if remaining or errors else "ok",
        "stored": stored,
        "requested": len(missing),
        "missing_symbols": sorted(remaining),
        "errors": errors,
        "run_id": str(ingestion.id),
        "checked_at": datetime.now(UTC).isoformat(),
    }
