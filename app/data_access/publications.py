"""Published PostgreSQL read models exposed to the API."""

from __future__ import annotations

from typing import Any

from investment_panel.database.authority import runtime_for_config
from investment_panel.database.options_analysis import published_options_radar_rows


def options_radar_rows(config: dict[str, Any], model_name: str) -> list[dict[str, Any]]:
    return published_options_radar_rows(runtime_for_config(config), model_name)
