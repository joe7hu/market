"""Application workflow for building and publishing the Market snapshot."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from investment_panel.core.market_time import market_timezone_for_symbol
from investment_panel.domain.decision import latest_completed_market_day
from investment_panel.infrastructure.postgres.confirmed_daily_prices import confirmed_daily_bars
from investment_panel.infrastructure.postgres.market_analysis import (
    build_market_publication,
    load_market_inputs,
    persist_market_publication,
)
from investment_panel.infrastructure.postgres.monitored_universe import monitored_universe
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime


TERMINAL_BAR_RETRY_SECONDS = 300


def terminal_bar_retry(
    runtime: DatabaseRuntime,
    configured_watchlist: list[dict[str, Any]] | None,
    as_of: datetime,
) -> dict[str, Any] | None:
    expected = latest_completed_market_day(as_of)
    universe = monitored_universe(runtime, configured_watchlist or [])
    if universe and not any(row["symbol"] == "QQQ" for row in universe):
        universe.append({"symbol": "QQQ", "asset_class": "etf"})
    eligible = [
        row for row in universe
        if row["asset_class"] in {"equity", "etf"}
        and market_timezone_for_symbol(str(row["symbol"])) == "America/New_York"
    ]
    if not eligible:
        return None
    symbols = [str(row["symbol"]).upper() for row in eligible]
    with runtime.read() as connection:
        instruments = {
            str(row["symbol"]).upper(): int(row["id"])
            for row in connection.execute(
                "SELECT id, symbol FROM catalog.instrument WHERE symbol = ANY(%s)", [symbols],
            ).fetchall()
        }
        bars = confirmed_daily_bars(
            connection,
            instruments.values(),
            as_of=as_of,
            trading_dates=[expected],
            require_session_close=True,
        )
    missing = [
        symbol for symbol in symbols
        if not bars.get(instruments.get(symbol, -1), [])
    ]
    if not missing:
        return None
    return {
        "status": "partial",
        "reason": "terminal_bar_retry",
        "expected_terminal_bar": expected.isoformat(),
        "missing_terminal_bars": sorted(missing),
        "retry_after_seconds": TERMINAL_BAR_RETRY_SECONDS,
    }


def refresh_market_publication(
    runtime: DatabaseRuntime,
    *,
    now: datetime | None = None,
    benchmark_symbols: list[str] | tuple[str, ...] | None = None,
    configured_watchlist: list[dict[str, Any]] | None = None,
    configured_watchlist_as_of: datetime | None = None,
    require_current_terminal_bars: bool = False,
    recheck_current_terminal_bars: bool = False,
) -> dict[str, Any]:
    """Run the read, compute, and publish stages for one market cutoff."""

    as_of = now or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ValueError("market publication timestamp must be timezone-aware")
    as_of = as_of.astimezone(UTC)
    for _attempt in range(2):
        if require_current_terminal_bars:
            retry = terminal_bar_retry(runtime, configured_watchlist, as_of)
            if retry is not None:
                return retry
        configured_at = configured_watchlist_as_of
        if configured_at is None and now is None:
            configured_at = as_of
        inputs = load_market_inputs(
            runtime,
            as_of=as_of,
            benchmark_symbols=benchmark_symbols,
            configured_watchlist=configured_watchlist,
            configured_watchlist_as_of=configured_at,
        )
        draft = build_market_publication(as_of=as_of, inputs=inputs)
        if not require_current_terminal_bars:
            return persist_market_publication(runtime, as_of=as_of, draft=draft)
        final_as_of = datetime.now(UTC) if recheck_current_terminal_bars else as_of
        retry = terminal_bar_retry(runtime, configured_watchlist, final_as_of)
        if retry is not None:
            return retry
        if latest_completed_market_day(final_as_of) == latest_completed_market_day(as_of):
            return persist_market_publication(runtime, as_of=as_of, draft=draft)
        as_of = final_as_of
    return {
        "status": "partial",
        "reason": "terminal_bar_cutoff_changed",
        "expected_terminal_bar": latest_completed_market_day(as_of).isoformat(),
        "missing_terminal_bars": [],
        "retry_after_seconds": TERMINAL_BAR_RETRY_SECONDS,
    }


__all__ = ["TERMINAL_BAR_RETRY_SECONDS", "refresh_market_publication", "terminal_bar_retry"]
