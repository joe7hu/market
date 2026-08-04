"""Small shared helpers for recovery-agent persistence and reporting."""

from __future__ import annotations

from typing import Any


def role(task: dict[str, Any]) -> str:
    request = dict(task.get("request") or {})
    return str(request.get("role") or str(task.get("task_kind") or "").removeprefix("options_recovery_"))


def number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def ratio(numerator: Any, denominator: Any) -> float | None:
    count = int(denominator or 0)
    return int(numerator or 0) / count if count else None


def empty_telemetry() -> dict[str, Any]:
    return {
        "cohort_id": None, "code_version": None,
        "batches": 0, "agent_failure_rate": None, "evidence_validation_rate": None,
        "unsupported_proposal_rate": None, "latency_seconds": None,
        "token_usage": {"input_tokens": 0, "output_tokens": 0},
        "advisory_lift_vs_deterministic_only": None,
        "advisory_lift_sample": {"with_agent_events": 0, "deterministic_only_events": 0},
    }
