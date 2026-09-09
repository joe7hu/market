"""Run the point-in-time continuous Market advisor through agent_task."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import math
import os
import time
from typing import Any

from investment_panel.core.agent_providers import provider_cost, resolve_provider_selection
from investment_panel.settings import AppConfig, load_config
from investment_panel.core.continuous_advisor import (
    CONTINUOUS_MAX_OUTPUT_TOKENS,
    build_evidence_packet,
    packet_fingerprint,
    validate_continuous_response,
)
from investment_panel.domain.decision import MARKET_TZ, is_market_open
from investment_panel.infrastructure.postgres.agent_context import ticker_context
from investment_panel.infrastructure.postgres.analysis import current_option_publication_answers
from investment_panel.infrastructure.postgres.authority import runtime_for_config
from investment_panel.infrastructure.postgres.continuous_advisor import ContinuousAdvisorRepository
from investment_panel.infrastructure.postgres.thesis import normalize_thesis_v3, thesis_monitor_rows
from investment_panel.jobs.codex_thesis_monitor import (
    generate_codex_continuous_advisor,
    generate_deepseek_continuous_advisor,
)
from investment_panel.jobs.run_thesis_monitor import validate_invalidations, validate_scenarios
from investment_panel.infrastructure.providers.advisory import AgentProviderError


def run(
    config_path: str | None = None,
    *,
    symbols: list[str] | None = None,
    now: datetime | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    config = load_config(config_path)
    settings = config.agents.thesis_monitor
    if not settings.continuous_enabled and not force:
        return {"status": "skipped", "reason": "continuous advisor disabled", "completed": 0, "failed": 0, "skipped": 0}
    scheduled_due = _scheduled_due_at() if now is None else None
    reference = now or scheduled_due or datetime.now(UTC)
    if reference.tzinfo is None:
        raise ValueError("continuous advisor now must be timezone-aware")
    reference = reference.astimezone(UTC)
    wall_clock = reference if now is not None else datetime.now(UTC)
    if scheduled_due is not None and not _scheduled_cutoff_is_current(scheduled_due, wall_clock):
        return {"status": "skipped", "reason": "scheduled_cutoff_expired", "completed": 0, "failed": 0, "skipped": 0}
    if not is_market_open(wall_clock):
        return {"status": "skipped", "reason": "market_closed", "completed": 0, "failed": 0, "skipped": 0}

    rows = selected_rows(config, symbols, cutoff=reference)
    runtime = runtime_for_config(config)
    repository = ContinuousAdvisorRepository(runtime)
    configured_prompt = str(settings.prompt_version or "thesis_v3_20260725")
    repository.ensure_prompt_version(
        {
            "version": configured_prompt,
            "template": {"schema": "continuous-advisor.v1", "authority": "research_ranking_only"},
            "approved_change_set": [],
            "mutation_rationale": "Initial configured continuous-advisor prompt.",
        }
    )
    prompt_version = repository.active_prompt_version(configured_prompt)
    prompt_template = repository.prompt_template(prompt_version)
    challenger = repository.prompt_status(configured_prompt).get("challenger")
    challenger_version = str(challenger.get("version") or "") if challenger else ""
    challenger_template = repository.prompt_template(challenger_version) if challenger_version else {}
    cadence = max(5, min(720, int(settings.continuous_cadence_minutes or 120)))
    cutoff = reference
    contexts: dict[str, dict[str, Any]] = {}
    with runtime.snapshot() as connection:
        current_option_rows = current_option_publication_answers(connection, cutoff=cutoff)
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            contexts[symbol] = ticker_context(
                connection,
                symbol,
                cutoff=cutoff,
                include_current_options=True,
                current_option_rows=current_option_rows,
            )

    completed = failed = skipped = 0
    spent = 0.0
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    for row in rows:
        result = _run_one(
            config,
            repository,
            row,
            contexts.get(str(row.get("symbol") or "").upper(), {}),
            cutoff=cutoff,
            prompt_version=prompt_version,
            prompt_template=prompt_template,
            cadence_minutes=cadence,
            budget_remaining=max(0.0, float(settings.continuous_budget_usd or 0) - spent),
            force=force,
            dry_run=dry_run,
        )
        if challenger_version and not dry_run and result.get("status") == "succeeded":
            challenger_result = _run_one(
                config,
                repository,
                row,
                contexts.get(str(row.get("symbol") or "").upper(), {}),
                cutoff=cutoff,
                prompt_version=challenger_version,
                prompt_template=challenger_template,
                cadence_minutes=cadence,
                budget_remaining=max(0.0, float(settings.continuous_budget_usd or 0) - spent - float(result.get("cost_usd") or 0)),
                force=False,
                dry_run=False,
                update_thesis=False,
            )
            result = {
                **result,
                "challenger": challenger_result,
                "cost_usd": float(result.get("cost_usd") or 0) + float(challenger_result.get("cost_usd") or 0),
            }
        results.append(result)
        status = str(result.get("status"))
        spent += float(result.get("cost_usd") or 0)
        if status == "succeeded":
            completed += 1
        elif status == "failed" or status == "invalid":
            failed += 1
            errors.append(f"{result.get('symbol')}: {result.get('error')}")
        else:
            skipped += 1
        challenger_result = result.get("challenger")
        if isinstance(challenger_result, dict) and challenger_result.get("status") in {"failed", "invalid"}:
            failed += 1
            errors.append(f"{challenger_result.get('symbol')} challenger: {challenger_result.get('error')}")
        if result.get("budget_unpriced") or result.get("budget_exceeded") or (
            isinstance(challenger_result, dict) and challenger_result.get("budget_unpriced")
        ) or (isinstance(challenger_result, dict) and challenger_result.get("budget_exceeded")):
            break
    return {
        "status": "ok" if failed == 0 else "partial" if completed else "failed",
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
        "dry_run": dry_run,
        "cutoff": cutoff,
        "prompt_version": prompt_version,
        "budget_usd": float(settings.continuous_budget_usd or 0),
        "spent_usd": round(spent, 6),
        "results": results,
        "errors": errors,
    }


def _run_one(
    config: AppConfig,
    repository: ContinuousAdvisorRepository,
    row: dict[str, Any],
    context: dict[str, Any],
    *,
    cutoff: datetime,
    prompt_version: str,
    prompt_template: dict[str, Any] | None = None,
    cadence_minutes: int,
    budget_remaining: float,
    force: bool,
    dry_run: bool,
    update_thesis: bool = True,
) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "").upper()
    packet = build_evidence_packet(
        row,
        context,
        cutoff=cutoff,
        prompt_version=prompt_version,
        cadence_minutes=cadence_minutes,
    )
    if dry_run:
        packet_row = {"id": None, **packet}
    else:
        packet_row = repository.store_packet(packet)
    due, due_reason = repository.should_review(
        packet_row,
        now=cutoff,
        stale_after_minutes=cadence_minutes,
    )
    if not due and not force:
        return {"symbol": symbol, "status": "skipped", "reason": due_reason, "packet_id": packet_row["id"]}
    if budget_remaining <= 0 and not dry_run:
        _record_skip(repository, packet_row, reason="budget_exhausted", config=config)
        return {"symbol": symbol, "status": "skipped", "reason": "budget_exhausted", "packet_id": packet_row["id"]}
    critical_blockers = {
        "cutoff_unavailable", "context_cutoff_after_packet_cutoff", "source_evidence_unavailable",
        "price_unavailable", "price_stale", "price_future", "price_untrusted", "price_unknown",
    }
    blockers = sorted(set(packet.get("blockers") or []))
    if critical_blockers.intersection(blockers):
        if not dry_run:
            _record_skip(repository, packet_row, reason=";".join(blockers), config=config)
        return {"symbol": symbol, "status": "skipped", "reason": "evidence_blocked", "blockers": blockers, "packet_id": packet_row["id"]}

    request = {
        "workflow": "continuous_advisor",
        "symbol": symbol,
        "packet_id": packet_row["id"],
        "packet_fingerprint": packet["fingerprint"],
        "cutoff": packet["cutoff"],
        "slot_start": packet["slot_start"],
        "prompt_version": prompt_version,
        "prompt_template": prompt_template or {},
        "reasoning_effort": config.agents.thesis_monitor.reasoning_effort,
        "evidence_packet": packet,
        "guardrails": packet["authority"],
        "max_output_tokens": CONTINUOUS_MAX_OUTPUT_TOKENS,
    }
    estimated_cost: float | None = None
    if not dry_run:
        estimated_cost = _estimated_call_cost(request, config)
        if estimated_cost is None:
            _record_skip(repository, packet_row, reason="budget_unpriced", config=config)
            return {"symbol": symbol, "status": "skipped", "reason": "budget_unpriced", "packet_id": packet_row["id"]}
        if budget_remaining < estimated_cost:
            _record_skip(repository, packet_row, reason="budget_exhausted", config=config)
            return {
                "symbol": symbol,
                "status": "skipped",
                "reason": "budget_exhausted",
                "budget_estimate_usd": estimated_cost,
                "packet_id": packet_row["id"],
            }
    if dry_run:
        try:
            output = _generator(config.agents.thesis_monitor.provider)(
                request,
                model=config.agents.thesis_monitor.model,
                reasoning_effort=config.agents.thesis_monitor.reasoning_effort,
            )
            validated = _validate_response(output, packet, row)
            return {"symbol": symbol, "status": "skipped", "reason": "dry_run_valid", "packet_id": packet_row["id"], "schema_valid": True, "claims": len(validated.get("forecasts") or [])}
        except Exception as exc:
            return {"symbol": symbol, "status": "failed", "reason": "dry_run_invalid", "error": f"{type(exc).__name__}: {exc}", "packet_id": packet_row["id"]}

    claim = repository.claim_review(
        packet_row,
        request={key: request[key] for key in ("workflow", "symbol", "packet_id", "packet_fingerprint", "cutoff", "slot_start", "prompt_version", "reasoning_effort")},
        provider=config.agents.thesis_monitor.provider,
        model=config.agents.thesis_monitor.model,
        reasoning_effort=config.agents.thesis_monitor.reasoning_effort,
    )
    if not claim["created"]:
        return {"symbol": symbol, "status": "skipped", "reason": claim["reason"], "task_id": claim["task_id"], "packet_id": packet_row["id"]}

    started = time.monotonic()
    output: dict[str, Any] = {}
    try:
        output = _generator(config.agents.thesis_monitor.provider)(
            request,
            model=config.agents.thesis_monitor.model,
            reasoning_effort=config.agents.thesis_monitor.reasoning_effort,
        )
        validated = _validate_response(output, packet, row)
        usage, cost = _telemetry(output, config)
        if cost is None:
            raise ValueError("provider cost unavailable; advisory call rejected")
        if cost > budget_remaining:
            raise ValueError("provider cost exceeded remaining advisory budget")
        thesis = normalize_thesis_v3(
            {**validated["thesis"], "countercase": validated["countercase"], "author_kind": "ai"},
            previous=dict(row.get("raw_thesis") or {}),
            symbol=symbol,
        )
        thesis_update = None
        if update_thesis and thesis != dict(row.get("raw_thesis") or {}):
            thesis_update = {
                **{
                    key: value for key, value in validated["thesis"].items()
                    if key not in {"automation_policy", "lifecycle_status"}
                },
                "countercase": validated["countercase"],
                "thesis": validated["thesis"]["core_thesis"],
                "author_kind": "ai",
                "source_agent_task_id": claim["task_id"],
                "change_rationale": validated.get("change_since_prior") or "Continuous advisor thesis update.",
            }
        latency_ms = max(0, int((time.monotonic() - started) * 1_000))
        finished = repository.finish_review(
            claim["task_id"],
            status="succeeded",
            response=validated,
            validation={"schema_valid": True, "evidence_valid": True, "packet_fingerprint": packet_fingerprint(packet)},
            usage=usage,
            cost_usd=cost,
            latency_ms=latency_ms,
            thesis_update=thesis_update,
            expected_thesis_revision=int(row.get("revision") or 0) if thesis_update is not None else None,
        )
        if finished.get("ignored"):
            return {
                "symbol": symbol,
                "status": "skipped",
                "reason": finished.get("reason") or "task_lease_lost",
                "task_id": claim["task_id"],
                "packet_id": packet_row["id"],
            }
        return {"symbol": symbol, "status": "succeeded", "task_id": claim["task_id"], "packet_id": packet_row["id"], "cost_usd": cost or 0, "latency_ms": latency_ms, "claims": len(validated.get("forecasts") or []) + len(validated.get("invalidations") or [])}
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        telemetry = exc.meta if isinstance(exc, AgentProviderError) else output
        usage, cost = _telemetry(telemetry, config)
        if isinstance(exc, AgentProviderError):
            provider_usage = exc.meta.get("usage") if isinstance(exc.meta.get("usage"), dict) else {}
            usage = {**provider_usage, **usage}
        finished = repository.finish_review(
            claim["task_id"],
            status="invalid" if isinstance(exc, ValueError) and not isinstance(exc, AgentProviderError) else "failed",
            response={},
            validation={"schema_valid": False, "evidence_valid": False},
            error=error,
            usage=usage,
            cost_usd=cost,
            latency_ms=max(0, int((time.monotonic() - started) * 1_000)),
        )
        if finished.get("ignored"):
            return {
                "symbol": symbol,
                "status": "skipped",
                "reason": finished.get("reason") or "task_lease_lost",
                "task_id": claim["task_id"],
                "packet_id": packet_row["id"],
            }
        repository.create_health_alert(symbol, error)
        return {
            "symbol": symbol,
            "status": "invalid" if isinstance(exc, ValueError) and not isinstance(exc, AgentProviderError) else "failed",
            "task_id": claim["task_id"],
            "packet_id": packet_row["id"],
            "error": error,
            "cost_usd": cost,
            "budget_unpriced": cost is None,
            "budget_exceeded": cost is not None and cost > budget_remaining,
            "budget_estimate_usd": estimated_cost,
        }


def _validate_response(output: dict[str, Any], packet: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    validated = validate_continuous_response(output, _provider_validation_packet(packet, output))
    validate_scenarios(validated["thesis"])
    validate_invalidations(validated["thesis"])
    return {**validated, "change_rationale": validated.get("change_since_prior") or "Continuous advisor assessment."}


def _provider_validation_packet(packet: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    """Validate evidence references against the exact provider-visible copy."""

    meta = output.get("_meta") if isinstance(output.get("_meta"), dict) else {}
    if "provider_evidence_references" not in meta:
        return packet
    refs = sorted({
        str(value).strip()
        for value in meta.get("provider_evidence_references") or []
        if str(value).strip()
    })
    provider_packet = dict(packet)
    evidence = dict(packet.get("evidence") or {})
    source_evidence = evidence.get("source_evidence")
    if isinstance(source_evidence, list):
        evidence["source_evidence"] = [
            item for item in source_evidence
            if isinstance(item, dict) and str(item.get("reference") or "").strip() in refs
        ]
    provider_packet["evidence"] = evidence
    provider_packet["source_references"] = refs
    provider_packet["fingerprint"] = packet_fingerprint(provider_packet)
    return provider_packet


def _record_skip(repository: ContinuousAdvisorRepository, packet: dict[str, Any], *, reason: str, config: AppConfig) -> None:
    claim = repository.claim_review(
        packet,
        request={
            "workflow": "continuous_advisor",
            "symbol": packet["symbol"],
            "packet_id": packet["id"],
            "packet_fingerprint": packet["fingerprint"],
            "cutoff": packet["cutoff"],
            "slot_start": packet["slot_start"],
            "prompt_version": packet["prompt_version"],
            "reasoning_effort": config.agents.thesis_monitor.reasoning_effort,
        },
        provider=config.agents.thesis_monitor.provider,
        model=config.agents.thesis_monitor.model,
        reasoning_effort=config.agents.thesis_monitor.reasoning_effort,
    )
    if claim["created"]:
        repository.finish_review(claim["task_id"], status="skipped", validation={"reason": reason})


def _generator(provider: str) -> Any:
    if str(provider).strip().lower() == "deepseek":
        return generate_deepseek_continuous_advisor
    return generate_codex_continuous_advisor


def _scheduled_due_at() -> datetime | None:
    raw = os.environ.get("MARKET_SCHEDULED_DUE_AT")
    if raw is None:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("MARKET_SCHEDULED_DUE_AT must be an ISO timestamp") from exc
    if value.tzinfo is None:
        raise ValueError("MARKET_SCHEDULED_DUE_AT must be timezone-aware")
    return value.astimezone(UTC)


def _scheduled_cutoff_is_current(scheduled_due: datetime, wall_clock: datetime) -> bool:
    return (
        wall_clock >= scheduled_due
        and wall_clock.astimezone(MARKET_TZ).date() == scheduled_due.astimezone(MARKET_TZ).date()
    )


def _telemetry(output: dict[str, Any], config: AppConfig) -> tuple[dict[str, Any], float | None]:
    meta = output.get("_meta") if isinstance(output.get("_meta"), dict) else output
    if not isinstance(meta, dict):
        meta = {}
    usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
    usage = {**usage, "reasoning_effort": config.agents.thesis_monitor.reasoning_effort}
    try:
        selection = resolve_provider_selection(
            config.agents.thesis_monitor.provider,
            config.agents.thesis_monitor.model,
            config.agents.thesis_monitor.reasoning_effort,
        )
        cost, _ = provider_cost(meta, selection=selection)
    except ValueError:
        cost = None
    return usage, cost


def _estimated_call_cost(request: dict[str, Any], config: AppConfig) -> float | None:
    """Reserve a conservative provider-rate estimate before each paid call."""

    try:
        selection = resolve_provider_selection(
            config.agents.thesis_monitor.provider,
            config.agents.thesis_monitor.model,
            config.agents.thesis_monitor.reasoning_effort,
        )
        input_tokens = max(1, math.ceil(len(json.dumps(request, default=str, separators=(",", ":"))) / 4))
        output_tokens = request.get("max_output_tokens") or CONTINUOUS_MAX_OUTPUT_TOKENS
        if isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or output_tokens <= 0:
            return None
        rate = selection.rate_card
        return round(
            input_tokens / 1_000_000 * rate.input_per_1m
            + output_tokens / 1_000_000 * rate.output_per_1m,
            6,
        )
    except (TypeError, ValueError, OverflowError):
        return None


def selected_rows(
    config: AppConfig,
    symbols: list[str] | None,
    *,
    cutoff: datetime | None = None,
) -> list[dict[str, Any]]:
    rows = [
        row for row in thesis_monitor_rows(config, as_of=cutoff)
        if row.get("owned") or row.get("watched")
    ]
    if not symbols:
        return rows
    wanted = {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}
    return [row for row in rows if str(row.get("symbol") or "").upper() in wanted]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--symbol", action="append", dest="symbols")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.config, symbols=args.symbols, force=args.force, dry_run=args.dry_run), indent=2, default=str))


if __name__ == "__main__":
    main()


__all__ = ["run"]
