"""Agent overview and on-demand analysis application actions."""

from __future__ import annotations

import os
from typing import Any, Callable

from investment_panel.database.agents import AgentRepository
from investment_panel.database.agent_experiments import AgentExperimentRepository
from investment_panel.database.authority import database_url, runtime_for_config


class AgentActions:
    def __init__(self, config: Any, start_job: Callable[[str, Any], dict[str, Any]]) -> None:
        self.config = config
        self.repository = AgentRepository(runtime_for_config(config))
        self.start_job = start_job

    def overview(self) -> dict[str, Any]:
        from investment_panel.core.config import config_to_dict

        settings = self.config if isinstance(self.config, dict) else config_to_dict(self.config)
        agents = dict(settings.get("agents") or {})
        overview = self.repository.overview()
        return {
            "config": agents.get("option_agent", {}),
            "pricing": agents.get("pricing", {}),
            "queue": overview["queue"],
            "runs": overview["runs"],
            "workflows": overview["workflows"],
            "cost": overview["cost"],
            "materialization": overview["materialization"],
            "scheduler": {"agent_refresh_seconds": _scheduler_agent_seconds(self.config)},
        }

    def queue_analysis(self, ticker: str, *, prompt: str = "") -> dict[str, Any]:
        normalized = str(ticker or "").strip().upper()
        if not normalized:
            raise ValueError("ticker is required")
        option_agent = _option_agent_settings(self.config)
        if not option_agent.get("command"):
            raise ValueError("Set the option agent command before running on-demand analysis.")
        request = self.repository.queue_thesis(normalized, prompt=prompt, trigger="ondemand")
        job = self.start_job("run_option_agents_ondemand", database_url(self.config))
        return {"ticker": normalized, "request_id": request["request_id"], "job": job}

    def current_experiment(self) -> dict[str, Any]:
        """Return the public experiment conclusion without operational batch rows."""

        summary = AgentExperimentRepository(runtime_for_config(self.config)).current()
        return summary or {
            "status": "not_started",
            "advisory_only": True,
            "routing_changed": False,
            "message": "No paired DeepSeek/Luna experiment has been queued.",
        }


def _scheduler_agent_seconds(config: Any) -> int:
    configured = int(_option_agent_settings(config).get("auto_run_seconds") or 0)
    if configured > 0:
        return configured
    try:
        return int(os.environ.get("MARKET_AGENT_REFRESH_SECONDS", "0") or 0)
    except ValueError:
        return 0


def _option_agent_settings(config: Any) -> dict[str, Any]:
    if isinstance(config, dict):
        return dict((config.get("agents") or {}).get("option_agent") or {})
    return {
        "command": config.agents.option_agent.command,
        "auto_run_seconds": config.agents.option_agent.auto_run_seconds,
    }
