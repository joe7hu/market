"""Daily premarket options-radar agent loop."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.options_radar import DEFAULT_STRATEGY_VERSION
from investment_panel.core.status import write_source_status
from investment_panel.jobs import refresh_options_radar, run_option_agents
from investment_panel.jobs.hourly_options_radar import app_is_serving_database


def run(config_path: str | None = None, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> dict[str, Any]:
    config = load_config(config_path)
    # The always-on app process owns the single DuckDB writer. If it is serving,
    # skip rather than block on the lock — the in-app scheduler runs the agent pass
    # in-process (set MARKET_AGENT_REFRESH_SECONDS) so theses stay fresh without this
    # external job deadlocking the API.
    if app_is_serving_database(config.database.duckdb_path):
        result = {
            "database": str(config.database.duckdb_path),
            "cadence": "daily_premarket",
            "status": "skipped_app_active",
            "agent_workers": "in_app_scheduler",
        }
        status_path = write_source_status(
            config,
            "mini-market-premarket-options-intelligence",
            {"source": "market-mini", "job": "premarket_options_intelligence", "origin": "autonomous_agent_worker", **result},
        )
        return {**result, "status_path": str(status_path) if status_path else None}
    before_agents = refresh_options_radar.run(config_path, strategy_version=strategy_version)
    agents = run_option_agents.run(config_path, strategy_version=strategy_version)
    after_agents = refresh_options_radar.run_deterministic_only(config_path, strategy_version=strategy_version)
    result = {
        "database": str(config.database.duckdb_path),
        "cadence": "daily_premarket",
        "agent_workers": "enabled_once_per_day",
        "strategy_version": strategy_version,
        "before_agents": before_agents,
        "agents": agents,
        "after_agents": after_agents,
    }
    status_path = write_source_status(
        config,
        "mini-market-premarket-options-intelligence",
        {
            "source": "market-mini",
            "job": "premarket_options_intelligence",
            "origin": "autonomous_agent_worker",
            **result,
        },
    )
    return {**result, "status_path": str(status_path) if status_path else None}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--strategy-version", default=DEFAULT_STRATEGY_VERSION)
    args = parser.parse_args()
    print(json.dumps(run(args.config, strategy_version=args.strategy_version), indent=2, default=str))


if __name__ == "__main__":
    main()
