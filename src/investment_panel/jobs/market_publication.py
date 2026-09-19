"""Rebuild Market from stored facts without rerunning successful collectors."""
from __future__ import annotations
from typing import Any
from investment_panel.settings import load_config
from investment_panel.infrastructure.postgres.authority import runtime_for_config
from investment_panel.workflows.market import refresh_market_publication


def run(config_path: str | None = "config.yaml") -> dict[str, Any]:
    config = load_config(config_path)
    return refresh_market_publication(runtime_for_config(config), configured_watchlist=config.watchlist)
