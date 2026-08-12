"""Run the advisory-only DeepSeek versus Luna paired experiment.

This worker is intentionally separate from the production option-agent queue.
It gives both providers the same frozen evidence packet and stores the output
only as experiment telemetry.  It never materializes a thesis, changes a
strategy, changes a ticket, or reaches a paper-order path.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from time import perf_counter
from typing import Any, Mapping
from uuid import UUID
from zoneinfo import ZoneInfo

from investment_panel.core.config import load_config
from investment_panel.core.decision import is_us_market_day
from investment_panel.core.agent_cost import estimate_agent_cost
from investment_panel.database.agent_candidate_queue import current_candidate_payloads
from investment_panel.database.agent_experiments import (
    AgentExperimentRepository,
)
from investment_panel.database.agent_process import validate_result
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.jobs.openai_option_agent import _postmortem_system_prompt, _thesis_system_prompt
from investment_panel.jobs.option_agent_contract import POSTMORTEM_SCHEMA, THESIS_SCHEMA
from investment_panel.jobs.provider_request import StructuredProviderRequest, invoke_structured


ROLE_ORDER = ("thesis_survival", "red_team", "postmortem", "mutation_draft")
MARKET_TZ = ZoneInfo("America/New_York")


def run(config_path: str | None = "config.yaml") -> dict[str, Any]:
    config = load_config(config_path)
    settings = config.agents.option_agent
    if not settings.experiment_enabled:
        return {
            "status": "skipped",
            "reason": "deepseek_luna_experiment_disabled",
            "advisory_only": True,
            "routing_changed": False,
        }
    runtime = runtime_for_config(config)
    return run_with_runtime(
        runtime,
        pricing=config.agents.pricing,
        timeout_seconds=settings.timeout_seconds,
    )


def run_with_runtime(
    runtime: DatabaseRuntime,
    *,
    pricing: Mapping[str, Any],
    timeout_seconds: int,
    now: datetime | None = None,
    max_pairs: int = 12,
) -> dict[str, Any]:
    """Queue current frozen evidence and execute at most twelve pairs.

    This helper takes a runtime so tests can prove both provider arms dispatch
    without network credentials.  The production wrapper above is the only
    scheduler entrypoint.
    """

    reference = _utc(now)
    if not is_us_market_day(reference.astimezone(MARKET_TZ).date()):
        return {
            "status": "skipped",
            "reason": "us_market_closed",
            "advisory_only": True,
            "routing_changed": False,
        }
    repository = AgentExperimentRepository(runtime)
    queued = _queue_current_pairs(runtime, repository, reference, limit=max_pairs)
    processed = _process_pairs(
        repository,
        pricing=pricing,
        timeout_seconds=timeout_seconds,
        now=reference,
        limit=max_pairs,
    )
    return {
        "status": "ok" if processed["failed"] == 0 else "partial",
        "queued_pairs": queued,
        **processed,
        "advisory_only": True,
        "routing_changed": False,
        "trade_control_mutation_allowed": False,
    }


def _queue_current_pairs(
    runtime: DatabaseRuntime,
    repository: AgentExperimentRepository,
    reference: datetime,
    *,
    limit: int,
) -> int:
    queued = 0
    for index, candidate in enumerate(current_candidate_payloads(runtime, limit=max(1, min(limit, 12)))):
        role = ROLE_ORDER[index % len(ROLE_ORDER)]
        packet = _evidence_packet(candidate)
        decision_id = _decision_id(candidate)
        result = repository.queue_pair(
            role=role,
            evidence_packet=packet,
            prompt_version="paired-experiment-v1",
            schema_version="option-agent-contract-v1",
            baseline_version="deterministic-option-controls-v1",
            decision_id=decision_id,
            now=reference,
        )
        queued += int(not result.get("idempotent_replay", False))
    return queued


def _process_pairs(
    repository: AgentExperimentRepository,
    *,
    pricing: Mapping[str, Any],
    timeout_seconds: int,
    now: datetime,
    limit: int,
) -> dict[str, Any]:
    completed = failed = pairs = 0
    errors: list[str] = []
    for _ in range(max(1, min(limit, 12))):
        claim = repository.claim_pair(now=now)
        if claim is None:
            break
        pairs += 1
        for task in claim["tasks"]:
            try:
                succeeded = _execute_task(
                    repository,
                    task,
                    pricing=pricing,
                    timeout_seconds=timeout_seconds,
                )
                if succeeded:
                    completed += 1
                else:
                    failed += 1
            except Exception as exc:  # pragma: no cover - defensive record failure path
                failed += 1
                errors.append(f"{task['id']}: {type(exc).__name__}: {exc}")
    return {
        "processed_pairs": pairs,
        "completed": completed,
        "failed": failed,
        "errors": errors,
    }


def _execute_task(
    repository: AgentExperimentRepository,
    task: Mapping[str, Any],
    *,
    pricing: Mapping[str, Any],
    timeout_seconds: int,
) -> bool:
    task_kind = str(task["task_kind"])
    schema, system_prompt = _contract(task_kind)
    request = dict(task.get("request") or {})
    packet = dict(request.get("evidence_packet") or {})
    provider = str(task.get("provider") or "").strip().lower()
    if provider not in {"codex", "deepseek"}:
        raise ValueError("experiment provider is invalid")
    meta: dict[str, Any] = {}
    started = perf_counter()
    try:
        output = invoke_structured(
            StructuredProviderRequest(
                provider=provider,  # type: ignore[arg-type]
                model=str(task["model"]),
                timeout_seconds=float(max(30, timeout_seconds)),
                reasoning_effort="high",
                schema_name=f"paired_{task_kind}",
                schema=schema,
                system_prompt=(
                    "This is an advisory-only paired evaluation. Use only the frozen evidence packet. "
                    "Do not propose price, quantity, READY, risk, promotion, or paper-order changes. "
                    "Use evidence_refs only for IDs present in the packet.\n\n"
                    f"{system_prompt}"
                ),
                payload={
                    "experiment_role": request.get("experiment_role"),
                    "evidence_packet": packet,
                    "evidence_fingerprint": task.get("evidence_fingerprint"),
                    "authority": "advisory_only",
                },
                compact=False,
            ),
            meta_sink=meta,
        )
        latency_ms = int((perf_counter() - started) * 1000)
        schema_errors = _schema_errors(output, schema)
        if not schema_errors:
            try:
                validate_result(task_kind, output)
            except ValueError as exc:
                schema_errors.append(str(exc))
        evidence_valid, evidence_reason = _evidence_valid(output, packet)
        useful = not schema_errors and evidence_valid and _useful_advice(task_kind, output)
        detail = {
            "schema_valid": not schema_errors,
            "schema_errors": schema_errors[:5],
            "evidence_valid": evidence_valid,
            "evidence_reason": evidence_reason,
            "useful_advice": useful,
            "baseline_version": task.get("baseline_version"),
            "advisory_only": True,
        }
        usage = dict(meta.get("usage") or {})
        repository.record_result(
            task_id=UUID(str(task["id"])),
            status="completed" if not schema_errors else "failed",
            validation_status="passed" if not schema_errors else "failed",
            validation_detail=detail,
            latency_ms=latency_ms,
            input_tokens=_integer(usage.get("input_tokens")),
            output_tokens=_integer(usage.get("output_tokens")),
            cost_usd=estimate_agent_cost(meta, dict(pricing)),
            result=output,
        )
        return not schema_errors
    except Exception as exc:
        latency_ms = int((perf_counter() - started) * 1000)
        usage = dict(meta.get("usage") or {})
        repository.record_result(
            task_id=UUID(str(task["id"])),
            status="failed",
            validation_status="failed",
            validation_detail={
                "schema_valid": False,
                "evidence_valid": False,
                "useful_advice": False,
                "error": f"{type(exc).__name__}: {exc}",
                "advisory_only": True,
            },
            latency_ms=latency_ms,
            input_tokens=_integer(usage.get("input_tokens")),
            output_tokens=_integer(usage.get("output_tokens")),
            cost_usd=estimate_agent_cost(meta, dict(pricing)),
        )
        return False


def _contract(task_kind: str) -> tuple[dict[str, Any], str]:
    if task_kind == "option_thesis":
        return THESIS_SCHEMA, _thesis_system_prompt()
    if task_kind == "option_postmortem":
        return POSTMORTEM_SCHEMA, _postmortem_system_prompt()
    raise ValueError("experiment task kind is invalid")


def _evidence_packet(candidate: Mapping[str, Any]) -> dict[str, Any]:
    ticket = dict(candidate.get("ticket") or {})
    legs = [
        {
            key: leg.get(key)
            for key in ("contract_id", "side", "option_type", "strike", "bid", "ask", "quote_time")
        }
        for leg in list(ticket.get("legs") or [])[:4]
        if isinstance(leg, Mapping)
    ]
    references = _dedupe_references([
        *_references(candidate.get("evidence"), explicit=True),
        *_references(candidate.get("source_evidence"), explicit=True),
        *_references(candidate.get("thesis_payload")),
    ])
    return {
        "decision_id": candidate.get("decision_id") or candidate.get("opportunity_id"),
        "symbol": str(candidate.get("ticker") or candidate.get("symbol") or "").upper(),
        "state": candidate.get("state"),
        "structure": candidate.get("structure") or ticket.get("structure"),
        "entry": dict(ticket.get("entry") or {}),
        "risk": dict(ticket.get("risk") or {}),
        "blockers": [str(value) for value in list(ticket.get("blockers") or candidate.get("blockers") or [])[:8]],
        "reasons": [str(value) for value in list(candidate.get("top_reasons") or candidate.get("reasons") or [])[:8]],
        "legs": legs,
        "evidence_refs": references,
        "deterministic_baseline": {
            "ticket_state": ticket.get("state"),
            "lower_confidence_expectancy_per_max_risk": candidate.get("lower_confidence_expectancy_per_max_risk"),
            "data_readiness": candidate.get("data_readiness"),
        },
    }


_REFERENCE_COLLECTION_KEYS = frozenset({
    "evidence_refs", "evidence_links", "source_evidence", "source_refs", "sources",
})


def _references(value: Any, *, explicit: bool = False) -> list[dict[str, str]]:
    """Extract only explicit evidence references, never arbitrary prose strings."""

    values: list[dict[str, str]] = []

    def add(reference: Any, reference_type: str = "evidence") -> None:
        text = str(reference or "").strip()
        if text:
            values.append({"type": reference_type or "evidence", "id": text})

    def collect(node: Any, reference_type: str = "evidence", *, explicit: bool = False) -> None:
        if isinstance(node, Mapping):
            node_type = str(node.get("type") or reference_type or "evidence")
            source_id = node.get("source_id") or node.get("url") or node.get("uri")
            if source_id:
                add(source_id, node_type)
            elif explicit and node.get("id"):
                add(node["id"], node_type)
            for key, nested in node.items():
                if str(key).lower() in _REFERENCE_COLLECTION_KEYS:
                    collect(nested, str(key).lower().removesuffix("s"), explicit=True)
                elif isinstance(nested, (Mapping, list)):
                    collect(nested, node_type)
        elif isinstance(node, list):
            for nested in node[:24]:
                collect(nested, reference_type, explicit=explicit)
        elif explicit:
            add(node, reference_type)

    collect(value, explicit=explicit)
    return _dedupe_references(values)


def _dedupe_references(values: list[dict[str, str]]) -> list[dict[str, str]]:
    unique_references = list(dict.fromkeys((row["type"], row["id"]) for row in values))
    return [
        {"type": reference_type, "id": reference_id}
        for reference_type, reference_id in unique_references
    ][:24]


def _evidence_valid(output: Mapping[str, Any], packet: Mapping[str, Any]) -> tuple[bool, str]:
    allowed = {
        (str(row.get("type")), str(row.get("id")))
        for row in list(packet.get("evidence_refs") or [])
        if isinstance(row, Mapping) and row.get("id")
    }
    supplied = {
        (str(row.get("type")), str(row.get("id")))
        for row in list(output.get("evidence_refs") or [])
        if isinstance(row, Mapping) and row.get("id")
    }
    if not allowed:
        return False, "frozen_evidence_has_no_stable_refs"
    if not supplied:
        return False, "agent_output_has_no_evidence_refs"
    if not supplied <= allowed:
        return False, "agent_output_references_unknown_evidence"
    return True, "passed"


def _useful_advice(task_kind: str, output: Mapping[str, Any]) -> bool:
    if task_kind == "option_thesis":
        return bool(str(output.get("core_thesis") or "").strip() and list(output.get("required_proofs") or []))
    return bool(str(output.get("proposed_rule_change") or "").strip() and list(output.get("evidence") or []))


def _schema_errors(value: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    types = schema.get("type")
    allowed_types = list(types) if isinstance(types, list) else [types] if types else []
    if allowed_types and not any(_matches_type(value, str(item)) for item in allowed_types):
        return [f"{path}: expected {' or '.join(str(item) for item in allowed_types)}"]
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return [f"{path}: value is not in enum"]
    errors: list[str] = []
    if isinstance(value, Mapping):
        required = [str(key) for key in list(schema.get("required") or [])]
        errors.extend(f"{path}.{key}: required" for key in required if key not in value)
        properties = dict(schema.get("properties") or {})
        if schema.get("additionalProperties") is False:
            errors.extend(f"{path}.{key}: additional property" for key in value if key not in properties)
        for key, child in properties.items():
            if key in value and isinstance(child, Mapping):
                errors.extend(_schema_errors(value[key], child, f"{path}.{key}"))
    elif isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                errors.extend(_schema_errors(item, item_schema, f"{path}[{index}]"))
    return errors


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def _decision_id(candidate: Mapping[str, Any]) -> UUID | None:
    value = candidate.get("decision_id") or candidate.get("opportunity_id")
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    return current.astimezone(UTC) if current.tzinfo is not None else current.replace(tzinfo=UTC)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.config), default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
