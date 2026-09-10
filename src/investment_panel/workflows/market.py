"""Application workflow for building and publishing the Market snapshot."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from investment_panel.infrastructure.postgres.market_analysis import (
    build_market_publication,
    load_market_inputs,
    persist_market_publication,
)
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime


def refresh_market_publication(
    runtime: DatabaseRuntime,
    *,
    now: datetime | None = None,
    benchmark_symbols: list[str] | tuple[str, ...] | None = None,
    configured_watchlist: list[dict[str, Any]] | None = None,
    configured_watchlist_as_of: datetime | None = None,
) -> dict[str, Any]:
    """Run the read, compute, and publish stages for one market cutoff."""

    as_of = now or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ValueError("market publication timestamp must be timezone-aware")
    as_of = as_of.astimezone(UTC)
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
    return persist_market_publication(runtime, as_of=as_of, draft=draft)


__all__ = ["refresh_market_publication"]
