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
    if option_agent.command and (option_agent.enabled or force or ondemand):
        queued = 0
        queued_postmortems = 0
        if not ondemand:
            queued = repository.queue_current_candidates(
                limit=max(0, int(option_agent.thesis_limit)),
                trigger="manual" if force else "scheduled",
            )
            queued_postmortems = repository.queue_current_postmortems(
                limit=max(0, int(option_agent.postmortem_limit)),
            )
        result = repository.run_queued(
            option_agent.command,
            limit=option_agent.thesis_limit + option_agent.postmortem_limit,
            timeout_seconds=option_agent.timeout_seconds,
            trigger=trigger,
            run_trigger=run_trigger,
            provider=option_agent.provider,
            model=option_agent.model,
            reasoning_effort=option_agent.reasoning_effort,
            consolidated=True,
            kind_limits={
                "option_thesis": option_agent.thesis_limit,
                "option_postmortem": option_agent.postmortem_limit,
            },
        )
        runner_status = str(result.get("status") or "failed")
        return {
            "ok": runner_status in {"ok", "skipped"}, "status": runner_status,
            "database": "postgresql", "strategy_version": strategy_version,
            "mode": "consolidated", "queued": queued,
            "queued_postmortems": queued_postmortems, "option_agent_runner": result,
        }

    thesis = repository.run_queued(
        config.agents.option_thesis.command if config.agents.option_thesis.enabled else "",
        limit=config.agents.option_thesis.limit,
        timeout_seconds=config.agents.option_thesis.timeout_seconds,
        trigger=trigger,
        task_kinds=("option_thesis",),
        model="option-thesis",
    )
    postmortem = repository.run_queued(
        config.agents.option_postmortem.command if config.agents.option_postmortem.enabled else "",
        limit=config.agents.option_postmortem.limit,
        timeout_seconds=config.agents.option_postmortem.timeout_seconds,
        trigger=trigger,
        task_kinds=("option_postmortem",),
        model="option-postmortem",
    )
    statuses = {str(thesis.get("status") or "failed"), str(postmortem.get("status") or "failed")}
    status = "failed" if statuses == {"failed"} else "partial" if statuses & {"failed", "partial"} else "ok"
    return {
        "ok": status == "ok", "status": status, "database": "postgresql",
        "strategy_version": strategy_version, "mode": "separate",
        "option_thesis": thesis, "option_postmortem": postmortem,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--strategy-version", default=DEFAULT_STRATEGY_VERSION)
    args = parser.parse_args()
    print(json.dumps(run(args.config, strategy_version=args.strategy_version), indent=2, default=str))


if __name__ == "__main__":
    main()
