"""Run guarded AI thesis-monitor automation against canonical PostgreSQL rows."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from typing import Any

from investment_panel.core.config import config_to_dict, load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.thesis import save_thesis, thesis_monitor_rows
from investment_panel.database.thesis_automation import ThesisAutomationRepository
from investment_panel.jobs.codex_thesis_monitor import OpenAIOptionAgentError, generate_codex_thesis_monitor


class ThesisAutomationValidationError(ValueError):
    """Raised when model output does not satisfy deterministic thesis rules."""


def run(
    config_path: str | None = None,
    *,
    symbols: list[str] | None = None,
    trigger: str = "manual",
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    config = load_config(config_path)
    config_dict = config_to_dict(config)
    settings = config.agents.thesis_monitor
    if not settings.enabled and not force and trigger != "ondemand":
        return {"status": "skipped", "reason": "thesis monitor automation disabled", "completed": 0, "failed": 0}
    rows = _selected_rows(config_dict, symbols)
    repository = ThesisAutomationRepository(runtime_for_config(config))
    completed = failed = skipped = 0
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    max_workers = max(1, min(2, int(settings.concurrency or 2)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                _run_one,
                config_dict,
                repository,
                row,
                trigger=trigger,
                force=force,
                dry_run=dry_run,
                model=settings.model or "configured_default",
                reasoning_effort=settings.reasoning_effort or "medium",
                prompt_version=settings.prompt_version,
                evidence_limit=max(1, int(settings.evidence_items_per_symbol or 12)),
                debounce_minutes=max(1, int(settings.debounce_minutes or 30)),
                max_material_runs_per_symbol_per_day=max(0, int(settings.max_material_runs_per_symbol_per_day or 2)),
            )
            for row in rows
        ]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            status = str(result.get("status"))
            if status == "succeeded":
                completed += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1
                errors.append(f"{result.get('symbol')}: {result.get('error')}")
    return {
        "status": "ok" if failed == 0 else "partial" if completed else "failed",
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
        "dry_run": dry_run,
        "results": sorted(results, key=lambda item: str(item.get("symbol"))),
        "errors": errors,
    }


def _run_one(
    config: dict[str, Any],
    repository: ThesisAutomationRepository,
    row: dict[str, Any],
    *,
    trigger: str,
    force: bool,
    dry_run: bool,
    model: str,
    reasoning_effort: str,
    prompt_version: str,
    evidence_limit: int,
    debounce_minutes: int,
    max_material_runs_per_symbol_per_day: int,
) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "").upper()
    evidence = list(row.get("source_evidence") or [])[:evidence_limit]
    eligible, reason = repository.eligible(
        symbol,
        trigger=trigger,
        debounce_minutes=debounce_minutes,
        max_material_runs_per_day=max_material_runs_per_symbol_per_day,
        force=force,
    )
    if not eligible:
        return {"symbol": symbol, "status": "skipped", "reason": reason}
    if dry_run:
        try:
            request = _request_payload(row, evidence, prompt_version=prompt_version)
            output = generate_codex_thesis_monitor(
                request,
                model=None if model == "configured_default" else model,
                reasoning_effort=reasoning_effort,
            )
            validate_model_output(output, row=row, evidence=evidence)
            return {"symbol": symbol, "status": "skipped", "reason": "dry_run_valid"}
        except (OpenAIOptionAgentError, ThesisAutomationValidationError, TimeoutError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            return {"symbol": symbol, "status": "failed", "error": error}
    run_id = repository.start_run(
        symbol,
        trigger=trigger,
        model=model,
        reasoning_effort=reasoning_effort,
        prompt_version=prompt_version,
        evidence_snapshot=evidence,
        status="running",
    )
    try:
        request = _request_payload(row, evidence, prompt_version=prompt_version)
        output = generate_codex_thesis_monitor(
            request,
            model=None if model == "configured_default" else model,
            reasoning_effort=reasoning_effort,
        )
        validated = validate_model_output(output, row=row, evidence=evidence)
        saved = save_thesis(
            config,
            symbol,
            {
                **validated["thesis"],
                "thesis": validated["thesis"]["core_thesis"],
                "author_kind": "ai",
                "automation_run_id": run_id,
                "change_rationale": validated["change_rationale"],
            },
        )
        assessments = repository.store_assessments(
            symbol,
            revision_id=int(saved["revision_id"]),
            run_id=run_id,
            assessments=validated["evidence_assessments"],
        )
        repository.finish_run(run_id, status="succeeded", usage=_usage(output))
        return {
            "symbol": symbol,
            "status": "succeeded",
            "run_id": run_id,
            "revision": saved["revision"],
            "assessments": assessments,
        }
    except (OpenAIOptionAgentError, ThesisAutomationValidationError, TimeoutError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        repository.finish_run(run_id, status="failed", error=error)
        repository.create_health_alert(symbol, title="Thesis automation failed", detail=error[:1000])
        return {"symbol": symbol, "status": "failed", "run_id": run_id, "error": error}


def validate_model_output(output: dict[str, Any], *, row: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "").upper()
    if str(output.get("symbol") or "").upper() != symbol:
        raise ThesisAutomationValidationError("model symbol does not match request")
    thesis = output.get("thesis")
    if not isinstance(thesis, dict) or not str(thesis.get("core_thesis") or "").strip():
        raise ThesisAutomationValidationError("model output missing thesis.core_thesis")
    allowed_refs = {str(item.get("reference")) for item in evidence if item.get("reference")}
    assessments = output.get("evidence_assessments")
    if not isinstance(assessments, list):
        raise ThesisAutomationValidationError("evidence_assessments must be an array")
    for item in assessments:
        if not isinstance(item, dict):
            raise ThesisAutomationValidationError("evidence assessment must be an object")
        ref = str(item.get("evidence_reference") or "")
        if ref not in allowed_refs:
            raise ThesisAutomationValidationError(f"hallucinated evidence reference: {ref}")
        stance = str(item.get("stance") or "")
        if stance not in {"support", "contradict", "neutral", "insufficient"}:
            raise ThesisAutomationValidationError(f"invalid evidence stance: {stance}")
        materiality = str(item.get("materiality") or "")
        if materiality not in {"low", "medium", "high"}:
            raise ThesisAutomationValidationError(f"invalid materiality: {materiality}")
        confidence = float(item.get("confidence") or 0)
        if confidence < 0 or confidence > 1:
            raise ThesisAutomationValidationError("assessment confidence out of bounds")
    _validate_scenarios(thesis)
    _validate_invalidations(thesis)
    if not allowed_refs:
        thesis["confidence"] = "low"
        thesis["evidence_coverage_status"] = thesis.get("evidence_coverage_status") or "low"
    thesis["schema_version"] = 3
    thesis["automation_policy"] = thesis.get("automation_policy") or row.get("automation_policy") or "auto"
    return {
        "symbol": symbol,
        "thesis": thesis,
        "evidence_assessments": assessments,
        "change_rationale": str(output.get("change_rationale") or "AI thesis monitor assessment."),
    }


def _validate_scenarios(thesis: dict[str, Any]) -> None:
    scenarios = thesis.get("scenarios")
    if not isinstance(scenarios, dict):
        raise ThesisAutomationValidationError("scenarios must be an object")
    probabilities = []
    for key in ("base", "bull", "bear"):
        item = scenarios.get(key)
        if not isinstance(item, dict):
            raise ThesisAutomationValidationError(f"scenario missing: {key}")
        probability = float(item.get("probability") or 0)
        if probability < 0 or probability > 1:
            raise ThesisAutomationValidationError(f"scenario probability out of bounds: {key}")
        probabilities.append(probability)
    if abs(sum(probabilities) - 1.0) > 0.02:
        raise ThesisAutomationValidationError("scenario probabilities must sum to 1")


def _validate_invalidations(thesis: dict[str, Any]) -> None:
    rules = thesis.get("invalidation_rules")
    if not isinstance(rules, list) or not rules:
        raise ThesisAutomationValidationError("at least one invalidation rule is required")
    for rule in rules:
        if not isinstance(rule, dict):
            raise ThesisAutomationValidationError("invalidation rule must be an object")
        rule_type = str(rule.get("type") or "")
        if rule_type not in {"price", "fundamental", "event", "time"}:
            raise ThesisAutomationValidationError(f"invalid invalidation type: {rule_type}")
        if rule_type == "price":
            if str(rule.get("operator") or "") not in {"<", "<=", ">", ">="}:
                raise ThesisAutomationValidationError("price invalidation requires an explicit operator")
            if rule.get("price") is None:
                raise ThesisAutomationValidationError("price invalidation requires price")


def _selected_rows(config: dict[str, Any], symbols: list[str] | None) -> list[dict[str, Any]]:
    rows = thesis_monitor_rows(config)
    if not symbols:
        return rows
    wanted = {str(symbol).upper() for symbol in symbols}
    return [row for row in rows if str(row.get("symbol") or "").upper() in wanted]


def _request_payload(row: dict[str, Any], evidence: list[dict[str, Any]], *, prompt_version: str) -> dict[str, Any]:
    return {
        "prompt_version": prompt_version,
        "symbol": row.get("symbol"),
        "current_thesis": row.get("raw_thesis") or {},
        "portfolio": {
            "owned": row.get("owned"),
            "portfolio_weight": row.get("portfolio_weight"),
            "market_value": row.get("market_value"),
            "unrealized_pnl": row.get("unrealized_pnl"),
        },
        "monitor_state": {
            "review_reason": row.get("review_reason"),
            "confidence": row.get("confidence"),
            "invalidation": row.get("invalidation"),
            "latest_price": row.get("latest_price"),
            "next_catalyst": row.get("next_catalyst"),
        },
        "evidence": evidence,
        "guardrails": {
            "authority": "research_ranking_only",
            "never_stage_orders": True,
            "never_clear_deterministic_gates": True,
            "use_only_evidence_references": [item.get("reference") for item in evidence if item.get("reference")],
        },
    }


def _usage(output: dict[str, Any]) -> dict[str, Any]:
    meta = output.get("_meta") if isinstance(output.get("_meta"), dict) else {}
    usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
    return usage


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--symbol", action="append", dest="symbols")
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.config, symbols=args.symbols, trigger=args.trigger, force=args.force, dry_run=args.dry_run), indent=2, default=str))


if __name__ == "__main__":
    main()
