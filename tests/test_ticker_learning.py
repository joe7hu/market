from __future__ import annotations

from investment_panel.core.decision.ticker_learning import evaluate_ticker_policy
from app.data_access.payloads import ticker_learning_payload


def test_ticker_policy_learning_fails_closed_without_cost_and_baseline_evidence() -> None:
    result = evaluate_ticker_policy([{
        "ticker_decision_id": "one",
        "ticker": "ACME",
        "horizon": "TACTICAL",
        "state": "resolved",
        "as_of": "2026-08-22T14:00:00Z",
        "selected_return": 0.04,
        "stock_counterfactual_return": 0.02,
        "metadata": {},
        "scenarios": [],
    }])

    assert result["status"] == "collecting"
    assert result["automatic_promotion"] is False
    assert "cost_adjusted_lower_95_expectancy_missing" in result["blockers"]
    assert "simple_trend_baseline_missing" in result["blockers"]
    assert "probability_calibration_coverage_missing" in result["blockers"]


def test_ticker_policy_learning_requires_paired_counterfactual_costs() -> None:
    rows = [
        {
            "ticker_decision_id": "episode-1",
            "ticker": "ACME",
            "horizon": "TACTICAL",
            "state": "resolved",
            "as_of": "2026-08-22T14:00:00Z",
            "selected_return": 0.04,
            "stock_counterfactual_return": 0.02,
            "metadata": {
                "cost_adjusted_selected_return": 0.03,
                "cost_adjusted_stock_counterfactual_return": 0.01,
                "trend_counterfactual_return": 0.005,
            },
            "scenarios": [{"name": "bull", "probability": 0.7}],
        },
        {
            "ticker_decision_id": "episode-2",
            "ticker": "BETA",
            "horizon": "TACTICAL",
            "state": "resolved",
            "as_of": "2026-08-23T14:00:00Z",
            "selected_return": 0.04,
            "stock_counterfactual_return": 0.02,
            "metadata": {
                "cost_adjusted_selected_return": 0.03,
                "cost_adjusted_cash_return": 0.0,
                "trend_counterfactual_return": 0.005,
            },
            "scenarios": [{"name": "bull", "probability": 0.7}],
        },
    ]

    result = evaluate_ticker_policy(rows)

    assert result["metrics"]["cost_adjusted_counterfactual_rows"] == 0
    assert "cost_adjusted_counterfactuals_missing" in result["blockers"]


def test_ticker_policy_learning_can_auto_promote_paper_policy_after_all_gates() -> None:
    rows = []
    for index in range(30):
        rows.append({
            "ticker_decision_id": f"episode-{index}",
            "ticker": f"T{index:02d}",
            "horizon": "TACTICAL",
            "state": "resolved",
            "as_of": f"2026-08-{index + 1:02d}T14:00:00Z" if index < 20 else f"2026-09-{index - 19:02d}T14:00:00Z",
            "selected_return": 0.03,
            "stock_counterfactual_return": 0.01,
            "metadata": {
                "cost_adjusted_selected_return": 0.02,
                "cost_adjusted_stock_counterfactual_return": 0.005,
                "cost_adjusted_cash_return": 0.0,
                "trend_counterfactual_return": 0.005,
                "sample": "historical" if index < 10 else "forward" if index < 20 else "canary",
                "purge_embargo_verified": True,
                "delistings_handled": True,
                "sector_slice": "technology",
                "regime_slice": "risk_on",
                "multiple_trial_correction": "max_t",
            },
            "scenarios": [{"name": "bull", "probability": 0.8}],
        })

    result = evaluate_ticker_policy(rows)

    assert result["status"] == "eligible"
    assert result["automatic_promotion"] is True
    assert result["blockers"] == []
    assert result["metrics"]["independent_episode_count"] == 30
    assert result["metrics"]["trading_day_count"] == 30


def test_ticker_policy_learning_rejects_overlapping_sample_intervals() -> None:
    rows = []
    for index, sample in enumerate(("historical", "forward", "canary")):
        rows.append({
            "ticker_decision_id": f"overlap-{index}",
            "ticker": f"O{index}",
            "horizon": "TACTICAL",
            "state": "resolved",
            "as_of": "2026-08-10T14:00:00Z",
            "selected_return": 0.02,
            "stock_counterfactual_return": 0.01,
            "metadata": {
                "sample": sample,
                "sample_start": "2026-08-01",
                "sample_end": "2026-08-20",
            },
            "scenarios": [{"name": "bull", "probability": 0.6}],
        })

    result = evaluate_ticker_policy(rows)

    assert "historical_forward_canary_overlap" in result["blockers"]
    assert result["metrics"]["sample_overlap_defects"] == 3


def test_ticker_policy_learning_does_not_count_placeholder_slices() -> None:
    rows = []
    for index in range(30):
        rows.append({
            "ticker_decision_id": f"placeholder-{index}",
            "ticker": f"P{index:02d}",
            "horizon": "TACTICAL",
            "state": "resolved",
            "as_of": f"2026-08-{index + 1:02d}T14:00:00Z" if index < 20 else f"2026-09-{index - 19:02d}T14:00:00Z",
            "selected_return": 0.03,
            "stock_counterfactual_return": 0.01,
            "metadata": {
                "cost_adjusted_selected_return": 0.02,
                "cost_adjusted_stock_counterfactual_return": 0.005,
                "cost_adjusted_cash_return": 0.0,
                "trend_counterfactual_return": 0.005,
                "sample": "historical" if index < 10 else "forward" if index < 20 else "canary",
                "purge_embargo_verified": True,
                "delistings_handled": True,
                "sector_slice": "technology",
                "regime_slice": "unknown",
                "multiple_trial_correction": "single-policy",
            },
            "scenarios": [{"name": "bull", "probability": 0.8}],
        })

    result = evaluate_ticker_policy(rows)

    assert result["metrics"]["regime_slice_rows"] == 0
    assert "regime_slice_evidence_missing" in result["blockers"]


def test_ticker_learning_payload_exposes_expression_result_and_policy_gate() -> None:
    payload = ticker_learning_payload(
        {
            "fundamental": {
                "evidence_for": [],
                "evidence_against": [],
                "fact_that_would_flip": {},
                "scenarios": [{"name": "bull", "probability": 0.6}],
            },
            "expressions": {
                "STOCK": {"selected": True, "status": "eligible", "planned_loss": 100},
            },
        },
        [{
            "ticker_decision_id": "episode-1",
            "horizon": "TACTICAL",
            "horizon_sessions": 1,
            "state": "resolved",
            "selected_return": 0.04,
            "stock_counterfactual_return": 0.04,
            "metadata": {"expression_returns": {"STOCK": 0.04}},
        }],
    )

    assert payload["strategy_learning"]["status"] == "collecting"
    assert payload["expression_tournament"][0]["outcomes"][0]["expression_return"] == 0.04
