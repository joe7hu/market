from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
from investment_panel.database.strategy_learning import StrategyLearningRepository


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
            "strategy_revision_id": "candidate-1", "database_verified": True,
            "paper_order_ids": [f"paper-{index}" for index in range(30)],
            "decision_ids": [f"decision-{index}" for index in range(30)],
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


class _LearningResult:
    def __init__(self, *, one=None, many=None) -> None:
        self.one = one
        self.many = many if many is not None else []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


def test_strategy_learning_does_not_reuse_parent_paper_execution_for_candidate() -> None:
    now = datetime(2026, 8, 30, 12, tzinfo=UTC)
    parent_row = {
        "as_of": now - timedelta(days=1), "spread_pct": 0.10, "dte": 30,
        "modeled_delta": 0.5, "iv_percentile": 0.4, "required_move_pct": 0.1,
        "open_interest": 100, "volume": 100, "peak_return": 0.1,
        "current_return": 0.1, "max_drawdown": -0.1, "probability_profit": 0.6,
        "ticker": "QQQ", "decision_id": "parent-decision", "strategy_revision_id": 42,
        "paper_order_id": "parent-paper", "paper_only": True, "paper_status": "exited",
        "filled_at": now - timedelta(days=1), "exit_at": now, "actual_fill_price": 100,
        "exit_price": 110, "filled_quantity": 1, "exited_quantity": 1,
        "entry_slippage": 0.1, "exit_slippage": 0.1, "fees": 0.5,
        "hit_rate_2x": 0.5, "false_positive_rate": 0.1,
    }

    class Connection:
        def __init__(self) -> None:
            self.evaluation_types = []

        def execute(self, query, params=None):
            if "SELECT id, created_at, result" in query:
                return _LearningResult(one={"id": 1, "created_at": now - timedelta(days=2), "result": {"candidate_revision_id": 43, "proposed_parameter_changes": {}}})
            if "SELECT candidate.parameters" in query:
                return _LearningResult(one={"parameters": {}, "supersedes_id": 42, "base_parameters": {}})
            if "FROM analysis.option_outcome" in query:
                return _LearningResult(many=[parent_row] if params == [42] else [])
            if "INSERT INTO analysis.strategy_evaluation" in query:
                self.evaluation_types.append(params[1])
            return _LearningResult()

    connection = Connection()
    repository = object.__new__(StrategyLearningRepository)
    repository._evaluate(connection, 1)
    assert connection.evaluation_types == ["walk_forward", "shadow"]
