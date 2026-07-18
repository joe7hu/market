"""Agent overview and on-demand analysis application actions."""

from __future__ import annotations

import os
from typing import Any, Callable

from investment_panel.database.agents import AgentRepository
from investment_panel.database.authority import runtime_for_config


class AgentActions:
    def __init__(self, config: Any, start_job: Callable[[str, Any], dict[str, Any]]) -> None:
        self.config = config
        self.repository = AgentRepository(runtime_for_config(config))
        self.start_job = start_job

    def overview(self) -> dict[str, Any]:
        from investment_panel.core.config import config_to_dict

        agents = config_to_dict(self.config)["agents"]
        overview = self.repository.overview()
        return {
            "config": agents.get("option_agent", {}),
            "pricing": agents.get("pricing", {}),
            "queue": overview["queue"],
            "runs": overview["runs"],
            "cost": overview["cost"],
            "scheduler": {"agent_refresh_seconds": _scheduler_agent_seconds(self.config)},
        }

    def queue_analysis(self, ticker: str, *, prompt: str = "") -> dict[str, Any]:
        normalized = str(ticker or "").strip().upper()
        if not normalized:
            raise ValueError("ticker is required")
        if not self.config.agents.option_agent.command:
            raise ValueError("Set the option agent command before running on-demand analysis.")
        request = self.repository.queue_thesis(normalized, prompt=prompt, trigger="ondemand")
        job = self.start_job("run_option_agents_ondemand", self.config.database.url)
        return {"ticker": normalized, "request_id": request["request_id"], "job": job}


def _scheduler_agent_seconds(config: Any) -> int:
    configured = int(config.agents.option_agent.auto_run_seconds or 0)
    if configured > 0:
        return configured
    try:
        return int(os.environ.get("MARKET_AGENT_REFRESH_SECONDS", "0") or 0)
    except ValueError:
        return 0
