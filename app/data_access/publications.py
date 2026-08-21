"""Published PostgreSQL read models exposed to the API."""

from __future__ import annotations

from typing import Any

from investment_panel.core.config import AppConfig
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.options_analysis import published_options_radar_rows
from investment_panel.database.option_ticket_read import (
    reconcile_radar_summary,
    revalidate_published_tickets,
)


def options_radar_rows(config: AppConfig, model_name: str) -> list[dict[str, Any]]:
    runtime = runtime_for_config(config)
    rows = published_options_radar_rows(runtime, model_name)
    if model_name not in {"option_radar_opportunity", "option_radar_summary"}:
        return rows
    sleeve = config.analysis.options_decision_system.options_risk_sleeve_capital
    opportunities = (
        rows
        if model_name == "option_radar_opportunity"
        else published_options_radar_rows(runtime, "option_radar_opportunity")
    )
    current = revalidate_published_tickets(runtime, opportunities, sleeve_capital=sleeve)
    return current if model_name == "option_radar_opportunity" else reconcile_radar_summary(rows, current)
