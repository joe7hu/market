from __future__ import annotations

from datetime import UTC, datetime

from investment_panel.core.decision.governance import (
    OUTCOME_ERROR_TYPES,
    TRACKED_METRICS,
    classify_outcome_error,
    classify_outcome_evidence,
    promotion_readiness,
    transition_dedupe_key,
)
from investment_panel.database import ticker_decisions
from investment_panel.database import decision_inbox


def _evaluation(stage: str, *, aliases: bool = False) -> dict[str, object]:
    metrics = {name: ({"risk_on": 0.5} if name == "regime_performance" else 0.1) for name in TRACKED_METRICS}
    if aliases:
        metrics["calibration_error"] = metrics.pop("calibration")
        metrics["precision_at_5"] = metrics.pop("precision_at_top_k")
    evidence = {
        "sample_size": 30,
        "source": "analysis.option_outcome",
        "method": "retained_actionable_decisions_forward_evaluation",
        "version": "phase7-governance-evidence-v1",
        "uncertainty": {"lower_95_expectancy": 0.01},
    }
    if stage == "execution_grade_paper":
        evidence["paper_execution"] = {
            "source": "app.paper_order", "paper_only": True,
            "sample_size": 30, "completed_orders": 30,
        }
    return {
        "stage": stage,
        "verdict": "pass",
        "evaluated_at": datetime(2026, 8, 30, 12, tzinfo=UTC),
        "available_at": datetime(2026, 8, 30, 12, tzinfo=UTC),
        "evidence": evidence,
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


def test_phase7_evidence_contract_rejects_renamed_and_non_paper_claims() -> None:
    renamed_rows = []
    for stage, field in zip(
        ("walk_forward", "shadow", "execution_grade_paper"), ("source", "method", "version"),
    ):
        renamed = _evaluation(stage)
        renamed["evidence"][field] = "renamed-value"
        renamed_rows.append(renamed)

    result = promotion_readiness(
        renamed_rows, now=datetime(2026, 8, 30, 13, tzinfo=UTC),
    )

    assert result["promotion_eligible"] is False
    assert "walk_forward_evidence_not_real" in result["blockers"]
    assert "execution_grade_paper_evidence_not_real" in result["blockers"]
    missing_paper = _evaluation("execution_grade_paper")
    missing_paper["evidence"].pop("paper_execution")
    missing_result = promotion_readiness(
        [_evaluation("walk_forward"), _evaluation("shadow"), missing_paper],
        now=datetime(2026, 8, 30, 13, tzinfo=UTC),
    )
    assert "execution_grade_paper_evidence_not_real" in missing_result["blockers"]


def test_phase7_metric_aliases_are_resolved_without_key_errors() -> None:
    rows = [_evaluation(stage, aliases=True) for stage in ("walk_forward", "shadow", "execution_grade_paper")]
    result = promotion_readiness(rows, now=datetime(2026, 8, 30, 13, tzinfo=UTC))
    assert result["promotion_eligible"] is True
    assert result["metrics"]["calibration"] == 0.1


def test_phase7_negative_finite_metric_is_not_promotion_evidence() -> None:
    rows = [_evaluation(stage) for stage in ("walk_forward", "shadow", "execution_grade_paper")]
    for row in rows:
        row["metrics"] = {
            name: ({"risk_on": -0.5} if name == "regime_performance" else -0.1)
            for name in TRACKED_METRICS
        }

    result = promotion_readiness(rows, now=datetime(2026, 8, 30, 13, tzinfo=UTC))

    assert result["promotion_eligible"] is False
    assert "walk_forward_calibration_malformed" in result["blockers"]


def test_phase7_transition_identity_cannot_collide_on_colons() -> None:
    assert transition_dedupe_key("a:b", "c", "newly_actionable", "policy") != transition_dedupe_key(
        "a", "b:c", "newly_actionable", "policy",
    )


def test_phase7_error_taxonomy_and_exact_notification_identity() -> None:
    assert set(OUTCOME_ERROR_TYPES) == {
        "forecast_error", "thesis_error", "regime_error", "timing_error",
        "expression_selection_error", "execution_slippage_error", "risk_sizing_error",
    }
    assert classify_outcome_error(expression_ok=False) == "expression_selection_error"
    assert classify_outcome_error() is None
    assert classify_outcome_evidence({"evidence_state": "OBSERVED", "checks": {"thesis_ok": False}}) is None
    assert classify_outcome_evidence({
        "evidence_state": "OBSERVED",
        "checks": {
            "forecast_ok": True, "thesis_ok": False, "regime_ok": True,
            "timing_ok": True, "expression_ok": True, "execution_ok": True,
            "sizing_ok": True,
        },
    }) == "thesis_error"
    assert transition_dedupe_key("episode", "revision", "newly_actionable", "policy") == transition_dedupe_key(
        "episode", "revision", "newly_actionable", "policy",
    )


def test_legacy_correct_outcomes_are_unavailable_not_phase7_errors() -> None:
    assert ticker_decisions._classify_mistake(
        stance="BEARISH", action="AVOID", selected_kind="CASH",
        selected_return=None, stock_return=0.10, alternate_return=None,
    ) == (None, {})
    assert ticker_decisions._classify_mistake(
        stance="BULLISH", action="BUY", selected_kind="OPTION",
        selected_return=None, stock_return=0.10, alternate_return=None,
    ) == (None, {})


def test_ticker_paper_dedupe_ignores_order_and_plan_identity() -> None:
    first = decision_inbox._ticker_paper_dedupe_key(
        "paper-order-1", "trade-plan-1", "revision-1", "policy-1", "paper_filled",
        episode_id="episode-1",
    )
    second = decision_inbox._ticker_paper_dedupe_key(
        "paper-order-2", "trade-plan-2", "revision-1", "policy-1", "paper_filled",
        episode_id="episode-1",
    )
    assert first == second
