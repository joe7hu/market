"""Run configured external agents against PostgreSQL queued tasks."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.database.options_constants import DEFAULT_STRATEGY_VERSION
from investment_panel.database.agents import AgentRepository
from investment_panel.database.authority import runtime_for_config


def run(
    config_path: str | None = None,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    force: bool = False,
    ondemand: bool = False,
) -> dict[str, Any]:
    config = load_config(config_path)
    repository = AgentRepository(runtime_for_config(config))
    option_agent = config.agents.option_agent
    trigger = "ondemand" if ondemand else None
    run_trigger = "ondemand" if ondemand else "manual" if force else "scheduled"
    if not option_agent.command or not (option_agent.enabled or force or ondemand):
        return {
            "ok": True,
            "status": "skipped",
            "database": "postgresql",
            "strategy_version": strategy_version,
            "mode": "consolidated",
            "reason": "option agent is disabled or command is not configured",
        }
    queued = 0
    queued_postmortems = 0
    if not ondemand:
        queued = repository.queue_current_candidates(
            limit=max(0, int(option_agent.thesis_limit)),
            trigger="manual" if force else "scheduled",
            context_sources=option_agent.context_sources,
        )
        queued_postmortems = repository.queue_current_postmortems(
            limit=max(0, int(option_agent.postmortem_limit)),
        )
    command = (
        f"{option_agent.command} --provider {option_agent.provider} --task batch"
    )
    result = repository.run_queued(
        command,
        limit=option_agent.thesis_limit + option_agent.postmortem_limit,
        timeout_seconds=option_agent.timeout_seconds,
        trigger=trigger,
        run_trigger=run_trigger,
        provider=option_agent.provider,
        model=option_agent.model,
        reasoning_effort=option_agent.reasoning_effort,
        max_runs_per_day=(
            int(option_agent.max_runs_per_day) if run_trigger == "scheduled" else 0
        ),
        consolidated=True,
        kind_limits={
            "option_thesis": option_agent.thesis_limit,
            "option_postmortem": option_agent.postmortem_limit,
        },
    )
    status = str(result.get("status") or "failed")
    return {
        "ok": status in {"ok", "skipped"},
        "status": status,
        "database": "postgresql",
        "strategy_version": strategy_version,
        "mode": "consolidated",
        "queued": queued,
        "queued_postmortems": queued_postmortems,
        "option_agent_runner": result,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--strategy-version", default=DEFAULT_STRATEGY_VERSION)
    args = parser.parse_args()
    print(json.dumps(run(args.config, strategy_version=args.strategy_version), indent=2, default=str))


if __name__ == "__main__":
    main()
