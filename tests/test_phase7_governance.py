from __future__ import annotations

from datetime import UTC, datetime

from investment_panel.core.decision.governance import (
    OUTCOME_ERROR_TYPES,
    TRACKED_METRICS,
    classify_outcome_error,
    promotion_readiness,
    transition_dedupe_key,
)


def _evaluation(stage: str) -> dict[str, object]:
    metrics = {name: ({"risk_on": 0.5} if name == "regime_performance" else 0.1) for name in TRACKED_METRICS}
    return {
        "stage": stage,
        "verdict": "pass",
        "evaluated_at": datetime(2026, 8, 30, 12, tzinfo=UTC),
        "available_at": datetime(2026, 8, 30, 12, tzinfo=UTC),
        "evidence": {"sample_size": 30, "source": "realized_paper_outcomes"},
        "metrics": metrics,
    }


def test_phase7_promotion_requires_all_real_stages_and_metrics() -> None:
    rows = [_evaluation(stage) for stage in ("walk_forward", "shadow", "execution_grade_paper")]
    result = promotion_readiness(rows, now=datetime(2026, 8, 30, 13, tzinfo=UTC))
    assert result["promotion_eligible"] is True
    assert result["paper_only"] is True
    assert result["live_eligibility"] == "unavailable"

    missing = promotion_readiness(rows[:2], now=datetime(2026, 8, 30, 13, tzinfo=UTC))
    assert missing["promotion_eligible"] is False
    assert "execution_grade_paper_evidence_missing" in missing["blockers"]


def test_phase7_malformed_or_legacy_claims_are_unavailable() -> None:
    row = _evaluation("walk_forward")
    row["evidence"] = {}
    row["metrics"] = {"brier": float("nan")}
    result = promotion_readiness([row], now=datetime(2026, 8, 30, 13, tzinfo=UTC))
    assert result["promotion_eligible"] is False
    assert "walk_forward_evidence_not_real" in result["blockers"]


def test_phase7_error_taxonomy_and_exact_notification_identity() -> None:
    assert set(OUTCOME_ERROR_TYPES) == {
        "forecast_error", "thesis_error", "regime_error", "timing_error",
        "expression_selection_error", "execution_slippage_error", "risk_sizing_error",
    }
    assert classify_outcome_error(expression_ok=False) == "expression_selection_error"
    assert classify_outcome_error() is None
    assert transition_dedupe_key("episode", "revision", "newly_actionable", "policy") == transition_dedupe_key(
        "episode", "revision", "newly_actionable", "policy",
    )
