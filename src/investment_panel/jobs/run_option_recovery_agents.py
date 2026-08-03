"""Run queued, advisory-only event recovery batches through Codex."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.options_recovery_agents import recovery_agent_schema, recovery_agent_system_prompt
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.options_recovery_agents import RecoveryEventAgentRepository
from investment_panel.jobs.openai_option_agent import _call_codex_structured


def run(
    config_path: str | None = "config.yaml",
    *,
    max_batches: int = 4,
    now: Any | None = None,
) -> dict[str, Any]:
    """Queue pre-open reviews when applicable, then process bounded batches.

    Deterministic collection never invokes this function inline.  Any Codex
    failure is persisted to agent telemetry and leaves event capture untouched.
    """

    config = load_config(config_path)
    settings = config.analysis.options_decision_system
    repository = RecoveryEventAgentRepository(runtime_for_config(config))
    preopen = repository.queue_preopen_reviews(
        now=now,
        model=config.agents.option_agent.model,
        reasoning_effort=config.agents.option_agent.reasoning_effort,
        debounce_minutes=settings.event_agent_debounce_minutes,
        max_batches_per_symbol_per_day=settings.event_agent_max_batches_per_symbol_per_day,
        max_tasks=settings.event_agent_max_tasks_per_batch,
    )
    results: list[dict[str, Any]] = []
    if not config.agents.option_agent.enabled:
        return {
            "status": "skipped", "reason": "option_agent_disabled", "preopen": preopen,
            "telemetry": repository.telemetry(), "batches": results,
        }
    for _ in range(max(0, min(int(max_batches), 12))):
        claim = repository.claim_next()
        if claim is None:
            break
        meta: dict[str, Any] = {}
        payload = {
            "batch": {
                "id": str(claim["batch"]["id"]),
                "trigger": claim["batch"]["trigger"],
                "fingerprint": claim["batch"]["fingerprint"],
                "authority": "advisory_only",
            },
            "tasks": [
                {"task_id": task["id"], "role": task["role"], "request": task["request"]}
                for task in claim["tasks"]
            ],
        }
        try:
            response = _call_codex_structured(
                payload,
                schema_name="options_recovery_event_batch",
                schema=recovery_agent_schema(),
                system_prompt=recovery_agent_system_prompt(),
                compact=False,
                meta_sink=meta,
                model=str(claim["batch"]["model"]),
                reasoning_effort=str(claim["batch"]["reasoning_effort"]),
                timeout=float(config.agents.option_agent.timeout_seconds),
            )
            results.append(repository.complete(claim, response, meta=meta))
        except Exception as exc:  # advisory failure is intentionally terminal only for this batch
            results.append(repository.fail(claim, exc, meta=meta))
    statuses = {str(result.get("status") or "failed") for result in results}
    status = "failed" if results and statuses == {"failed"} else "partial" if "failed" in statuses else "ok"
    return {
        "status": status, "database": "postgresql", "preopen": preopen,
        "batches": results, "telemetry": repository.telemetry(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--max-batches", type=int, default=4)
    args = parser.parse_args()
    print(json.dumps(run(args.config, max_batches=args.max_batches), indent=2, default=str))


if __name__ == "__main__":
    main()
