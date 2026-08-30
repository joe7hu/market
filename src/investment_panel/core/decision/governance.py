"""Backend-owned learning governance and transition rules.

This module is deliberately small.  It validates evidence before it can be
used by a promotion or notification decision; missing evidence is never
silently treated as a failed or successful observation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import math
from typing import Any, Mapping


GOVERNANCE_STAGES = (
    "backtest",
    "walk_forward",
    "shadow",
    "execution_grade_paper",
    "limited_live",
)
PROMOTION_STAGES = ("walk_forward", "shadow", "execution_grade_paper")
CANONICAL_EVIDENCE_SOURCE = "analysis.option_outcome"
CANONICAL_EVIDENCE_METHOD = "retained_actionable_decisions_forward_evaluation"
CANONICAL_EVIDENCE_VERSION = "phase7-governance-evidence-v1"
PAPER_EXECUTION_EVIDENCE_SOURCE = "app.paper_order"
TRACKED_METRICS = (
    "calibration",
    "brier",
    "log_loss",
    "precision_at_top_k",
    "net_pnl_after_modeled_costs",
    "net_pnl_after_realized_costs",
    "drawdown",
    "tail_loss",
    "turnover",
    "slippage",
    "capacity",
    "regime_performance",
    "false_positives",
    "missed_winners",
)
OUTCOME_ERROR_TYPES = (
    "forecast_error",
    "thesis_error",
    "regime_error",
    "timing_error",
    "expression_selection_error",
    "execution_slippage_error",
    "risk_sizing_error",
)
NOTIFIABLE_TRANSITIONS = (
    "newly_actionable",
    "action_changed",
    "thesis_invalidated",
    "risk_limit_breached",
    "staged",
    "filled",
    "exited",
    "blocking_data_degradation",
)
def promotion_readiness(
    evaluations: Mapping[str, Any] | list[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(days=365),
) -> dict[str, Any]:
    """Return a fail-closed, paper-only promotion governance assessment.

    The three required stages need real, fresh, structured evidence.  A row
    can use either a flat metrics object or a nested ``metrics`` object.  A
    stage is not considered real when it only carries a verdict.
    """

    reference = _aware(now or datetime.now(UTC))
    rows = _evaluation_rows(evaluations)
    stages: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []
    for stage in PROMOTION_STAGES:
        row = rows.get(stage)
        stage_result: dict[str, Any] = {"stage": stage, "status": "unavailable", "metrics": {}}
        if row is None:
            blockers.append(f"{stage}_evidence_missing")
            stages[stage] = stage_result
            continue
        metrics = _metrics(row)
        stage_result["metrics"] = metrics
        stage_result["evaluated_at"] = row.get("evaluated_at")
        stage_result["available_at"] = row.get("available_at")
        if str(row.get("verdict") or row.get("status") or "").lower() not in {"pass", "passed", "available"}:
            blockers.append(f"{stage}_evidence_not_passed")
            stages[stage] = stage_result
            continue
        if not _real_evidence(row, stage=stage):
            blockers.append(f"{stage}_evidence_not_real")
            stages[stage] = stage_result
            continue
        if not _fresh(row, reference, max_age):
            blockers.append(f"{stage}_evidence_stale")
            stages[stage] = stage_result
            continue
        missing = [name for name in TRACKED_METRICS if not _metric_present(metrics, name)]
        malformed = [
            name for name in TRACKED_METRICS
            if _metric_present(metrics, name)
            and not _metric_domain_valid(name, _metric_value(metrics, name))
        ]
        if missing:
            blockers.extend(f"{stage}_{name}_missing" for name in missing)
        if malformed:
            blockers.extend(f"{stage}_{name}_malformed" for name in malformed)
        if not missing and not malformed:
            stage_result["status"] = "available"
        else:
            stage_result["status"] = "advisory"
        stages[stage] = stage_result
    ready = not blockers
    return {
        "status": "eligible" if ready else "unavailable",
        "promotion_eligible": ready,
        "paper_only": True,
        "live_eligibility": "unavailable",
        "live_blocker": "market_paper_only_policy",
        "stages": stages,
        "metrics": {
            name: (
                None
                if (value := _metric_value(stages.get("execution_grade_paper", {}).get("metrics", {}), name)) is _MISSING
                else value
            )
            for name in TRACKED_METRICS
        },
        "blockers": list(dict.fromkeys(blockers)),
    }


def classify_outcome_error(
    *,
    forecast_ok: bool | None = None,
    thesis_ok: bool | None = None,
    regime_ok: bool | None = None,
    timing_ok: bool | None = None,
    expression_ok: bool | None = None,
    execution_ok: bool | None = None,
    sizing_ok: bool | None = None,
) -> str | None:
    """Classify only an evidenced error; no evidence returns ``None``."""

    for value, name in (
        (forecast_ok, "forecast_error"), (thesis_ok, "thesis_error"),
        (regime_ok, "regime_error"), (timing_ok, "timing_error"),
        (expression_ok, "expression_selection_error"),
        (execution_ok, "execution_slippage_error"), (sizing_ok, "risk_sizing_error"),
    ):
        if value is False:
            return name
    return None


def valid_outcome_error_type(value: Any) -> str | None:
    """Return only an exact Phase 7 error label from persisted data."""

    return value if isinstance(value, str) and value in OUTCOME_ERROR_TYPES else None


def classify_outcome_evidence(evidence: Mapping[str, Any] | None) -> str | None:
    """Classify a validated outcome only when every check is explicit.

    Production callers pass observations from the canonical outcome seam.  A
    missing check is advisory and therefore cannot manufacture a taxonomy
    label.
    """

    if not isinstance(evidence, Mapping) or str(evidence.get("evidence_state") or "").upper() not in {
        "OBSERVED", "REALIZED", "RESOLVED",
    }:
        return None
    checks = evidence.get("checks")
    if not isinstance(checks, Mapping):
        return None
    names = (
        "forecast_ok", "thesis_ok", "regime_ok", "timing_ok",
        "expression_ok", "execution_ok", "sizing_ok",
    )
    if any(name not in checks or not isinstance(checks[name], bool) for name in names):
        return None
    values = {name: checks[name] for name in names}
    return classify_outcome_error(**values)


def transition_dedupe_key(
    episode_id: str,
    decision_revision: str,
    transition: str,
    policy_version: str,
) -> str:
    if transition not in NOTIFIABLE_TRANSITIONS:
        raise ValueError("unsupported governance notification transition")
    values = (episode_id.strip(), decision_revision.strip(), transition, policy_version.strip())
    if not all(values):
        raise ValueError("governance notification identity is incomplete")
    # JSON encodes tuple fields with length/quoting semantics, so delimiters
    # inside an identity cannot collide with delimiters between identities.
    return "phase7:" + json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _evaluation_rows(value: Mapping[str, Any] | list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    items = value.items() if isinstance(value, Mapping) else ((None, row) for row in value)
    result: dict[str, dict[str, Any]] = {}
    for key, raw in items:
        if not isinstance(raw, Mapping):
            continue
        stage = str(raw.get("stage") or raw.get("evaluation_type") or key or "").strip().lower().replace("-", "_")
        if stage in PROMOTION_STAGES and stage not in result:
            result[stage] = dict(raw)
    return result


def _metrics(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("metrics")
    if not isinstance(value, Mapping):
        value = row
    return dict(value)


def _real_evidence(row: Mapping[str, Any], *, stage: str | None = None) -> bool:
    evidence = row.get("evidence")
    if isinstance(evidence, Mapping):
        return _evidence_item_real(evidence, stage=stage)
    if isinstance(evidence, list):
        return bool(evidence) and all(
            isinstance(item, Mapping) and _evidence_item_real(item, stage=stage)
            for item in evidence
        )
    return False


def _evidence_item_real(evidence: Mapping[str, Any], *, stage: str | None) -> bool:
    if (
        evidence.get("source") != CANONICAL_EVIDENCE_SOURCE
        or evidence.get("method") != CANONICAL_EVIDENCE_METHOD
        or evidence.get("version") != CANONICAL_EVIDENCE_VERSION
        or not _positive_int(evidence.get("sample_size"))
    ):
        return False
    uncertainty = evidence.get("uncertainty")
    if not isinstance(uncertainty, Mapping) or not uncertainty:
        return False
    if not any(_metric_valid(value) for value in uncertainty.values()):
        return False
    if stage != "execution_grade_paper":
        return True
    paper = evidence.get("paper_execution")
    if not isinstance(paper, Mapping):
        return False
    return (
        paper.get("source") == PAPER_EXECUTION_EVIDENCE_SOURCE
        and paper.get("paper_only") is True
        and _positive_int(paper.get("sample_size"))
        and paper.get("sample_size") == evidence.get("sample_size")
        and _positive_int(paper.get("completed_orders"))
        and paper.get("completed_orders") == paper.get("sample_size")
    )


def _fresh(row: Mapping[str, Any], reference: datetime, max_age: timedelta) -> bool:
    raw = row.get("available_at") or row.get("evaluated_at")
    if raw is None:
        return False
    try:
        observed = _aware(raw)
    except (TypeError, ValueError):
        return False
    return observed <= reference and reference - observed <= max_age


def _metric_present(metrics: Mapping[str, Any], name: str) -> bool:
    return _metric_value(metrics, name) is not _MISSING


_MISSING = object()


def _metric_value(metrics: Mapping[str, Any], name: str) -> Any:
    if name in metrics:
        return metrics[name]
    aliases = {
        "calibration": ("calibration_error", "calibration_metrics"),
        "precision_at_top_k": ("precision_at_5",),
        "net_pnl_after_modeled_costs": ("net_expectancy", "net_pnl"),
        "net_pnl_after_realized_costs": ("realized_net_pnl",),
        "drawdown": ("max_drawdown",),
        "tail_loss": ("max_tail_loss",),
        "regime_performance": ("regime_slice",),
        "false_positives": ("false_positive_rate",),
        "missed_winners": ("missed_winner_rate",),
    }
    for alias in aliases.get(name, ()):
        if alias in metrics:
            return metrics[alias]
    return _MISSING


def _metric_valid(value: Any) -> bool:
    if isinstance(value, Mapping):
        return bool(value) and all(_metric_valid(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return bool(value) and all(_metric_valid(item) for item in value)
    if isinstance(value, bool) or value is None:
        return value is not None
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    return False


def _metric_domain_valid(name: str, value: Any) -> bool:
    """Reject finite values outside the domain accepted by governance."""
    if not _metric_valid(value):
        return False
    if name in {"drawdown", "tail_loss"}:
        return True
    if isinstance(value, Mapping):
        return all(_metric_domain_valid(name, item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_metric_domain_valid(name, item) for item in value)
    try:
        return float(value) >= 0.0
    except (TypeError, ValueError, OverflowError):
        return False


def _positive_int(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return int(value) > 0 and float(value) == int(value)
    except (TypeError, ValueError, OverflowError):
        return False


def _aware(value: Any) -> datetime:
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("governance timestamp must be timezone-aware")
    return value.astimezone(UTC)
