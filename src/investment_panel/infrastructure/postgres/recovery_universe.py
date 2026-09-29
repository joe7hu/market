"""One bounded recovery denominator shared by reference collection and detection."""

from __future__ import annotations

import os
from typing import Any

from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.option_events import OptionEventRepository


def detector_symbol_limit(configured_limit: int) -> int:
    raw = os.environ.get("MARKET_ROBINHOOD_MAX_SYMBOLS")
    try:
        override = int((raw or "").strip())
    except (TypeError, ValueError):
        override = 0
    return max(1, override if override > 0 else int(configured_limit))


def detector_universe(
    ingestion: IngestionRepository,
    repository: OptionEventRepository,
    *,
    configured: list[dict[str, Any]],
    limit: int,
) -> tuple[list[str], list[str]]:
    active = repository.current_event_symbols(limit=limit)
    prioritized = [{"symbol": symbol} for symbol in active]
    prioritized.extend(configured)
    discovered = ingestion.option_universe(prioritized, limit=limit)
    return list(dict.fromkeys([*active, *discovered]))[:limit], active
