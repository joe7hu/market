"""Refresh broad-market valuation inputs and publish the Market page."""

from __future__ import annotations

from typing import Any

from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.market_analysis import refresh_market_publication
from investment_panel.jobs import update_market_valuations


def run(config_path: str | None = None) -> dict[str, Any]:
    """Keep Market's valuation charts current without a DuckDB fallback."""

    config = load_config(config_path)
    valuations = update_market_valuations.run(config_path)
    if not valuations.get("ok"):
        return valuations
    market = refresh_market_publication(runtime_for_config(config))
    ok = market.get("status") == "ok"
    return {
        "status": "ok" if ok else "failed",
        "ok": ok,
        "database": "postgresql",
        "valuations": valuations,
        "market_publication": market,
    }
