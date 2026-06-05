"""Run configured external option-radar agents for open handoff requests."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db
from investment_panel.core.option_agent_runner import run_external_option_agents
from investment_panel.core.options_radar import DEFAULT_STRATEGY_VERSION


def run(config_path: str | None = None, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> dict[str, Any]:
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path, read_only=False) as con:
        result = run_external_option_agents(
            con,
            strategy_version=strategy_version,
            thesis_command=config.agents.option_thesis.command if config.agents.option_thesis.enabled else "",
            thesis_limit=config.agents.option_thesis.limit,
            thesis_timeout_seconds=config.agents.option_thesis.timeout_seconds,
            postmortem_command=config.agents.option_postmortem.command if config.agents.option_postmortem.enabled else "",
            postmortem_limit=config.agents.option_postmortem.limit,
            postmortem_timeout_seconds=config.agents.option_postmortem.timeout_seconds,
        )
    return {"database": str(config.database.duckdb_path), **result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--strategy-version", default=DEFAULT_STRATEGY_VERSION)
    args = parser.parse_args()
    print(json.dumps(run(args.config, strategy_version=args.strategy_version), indent=2, default=str))


if __name__ == "__main__":
    main()
