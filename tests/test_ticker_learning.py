from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from investment_panel.core.decision.ticker_learning import evaluate_ticker_policy
from investment_panel.core.decision import ExpressionKind, InputLineage, OutcomeAttribution
from app.data_access.payloads import ticker_learning_payload


def _canonical_rows(*, sample_eligible: bool = True, promotion_eligible: bool = True) -> list[dict[str, object]]:
    cutoff = datetime(2026, 8, 22, 14, tzinfo=UTC)
    observed = cutoff + timedelta(days=1)
    rows = []
    for horizon, sessions in (
        ("TACTICAL", 1), ("TACTICAL", 5), ("TACTICAL", 20),
        ("FUNDAMENTAL", 63), ("FUNDAMENTAL", 126), ("FUNDAMENTAL", 252),
    ):
        plan_id = "trade-plan.v1:learning"
        stock = {
            "kind": "STOCK", "source_id": "confirmed_price_bar",
            "observed_at": observed, "available_at": observed,
            "gross_return": 0.01, "cost_adjusted_return": 0.005,
            "evidence_state": "OBSERVED",
        }
        value = {
            "stable_unit_key": f"{plan_id}:{horizon}:{sessions}",
            "ticker": "ACME", "trade_plan_id": plan_id,
            "trade_plan_publication_id": "publication:learning",
            "opportunity_episode_id": "episode:learning",
            "decision_revision": "revision:learning", "policy_version": "policy:v1",
            "selected_expression_kind": "STOCK",
            "selected_expression_identity": "expression:learning",
            "decision_cutoff": cutoff, "evaluation_cutoff": observed,
            "horizon": horizon, "horizon_sessions": sessions,
            "state": "RESOLVED", "observed_through": observed, "available_at": observed,
            "outcome_evidence": [stock], "selected_evidence": stock,
            "selected_gross_return": 0.03, "selected_net_return": 0.02,
            "counterfactuals": {"STOCK": stock, "CASH": {"kind": "CASH", "gross_return": 0.0}},
            "all_expression_counterfactuals": {"STOCK": stock, "CASH": {"kind": "CASH", "gross_return": 0.0}},
            "evidence_state": "OBSERVED", "sample_eligible": sample_eligible,
            "promotion_eligible": promotion_eligible,
            "paper_execution": {
                "trade_plan_id": plan_id, "status": "EXITED", "paper_only": True,
                "entry_filled_at": observed, "exit_at": observed,
                "entry_fill_price": 100, "exit_price": 103,
                "filled_quantity": 1, "exited_quantity": 1,
                "entry_fill_count": 1, "exit_fill_count": 1,
                "realized_gross_return": 0.03, "realized_net_return": 0.02,
                "available_at": observed,
            },
        }
        rows.append(OutcomeAttribution.model_validate(value).model_dump(mode="json"))
    return rows


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


def test_canonical_learning_rejects_forged_and_non_finite_attributions() -> None:
    rows = _canonical_rows()
    forged = {**rows[0], "selected_net_return": 0.01}
    non_finite = {**rows[1], "selected_net_return": float("nan")}

    result = evaluate_ticker_policy([forged, non_finite, *rows[2:]], canonical_only=True)

    assert result["automatic_promotion"] is False
    assert "outcome_attribution_invalid" in result["blockers"]
    assert "outcome_attribution_non_finite" in result["blockers"]


def test_canonical_learning_rejects_duplicate_and_incomplete_units() -> None:
    rows = _canonical_rows()

    duplicate = evaluate_ticker_policy([*rows, rows[0]], canonical_only=True)
    incomplete = evaluate_ticker_policy(rows[:-1], canonical_only=True)

    assert "outcome_attribution_unit_duplicated" in duplicate["blockers"]
    assert duplicate["automatic_promotion"] is False
    assert "outcome_attribution_units_incomplete" in incomplete["blockers"]
    assert incomplete["automatic_promotion"] is False


def test_canonical_learning_keeps_sample_evidence_separate_from_promotion() -> None:
    result = evaluate_ticker_policy(
        _canonical_rows(promotion_eligible=False), canonical_only=True,
    )

    assert result["metrics"]["canonical_sample_eligible_rows"] == 6
    assert "canonical_promotion_evidence_missing" in result["blockers"]
    assert result["automatic_promotion"] is False


def test_legacy_learning_payload_cannot_promote_without_canonical_evidence() -> None:
    payload = ticker_learning_payload(
        {"fundamental": {}, "expressions": {}},
        [{"ticker_decision_id": "legacy", "state": "resolved", "horizon": "TACTICAL"}],
    )

    assert payload["strategy_learning"]["automatic_promotion"] is False
    assert "canonical_outcome_attribution_missing" in payload["strategy_learning"]["blockers"]


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


def test_repository_learning_surface_rejects_mismatched_current_attributions(monkeypatch) -> None:
    from investment_panel.database import ticker_decisions as owner

    cutoff = datetime(2026, 8, 22, 14, tzinfo=UTC)
    old_lineage = InputLineage(
        field="old-source", source_id="old", source_version="1",
        event_at=cutoff - timedelta(minutes=5),
        available_at=cutoff - timedelta(minutes=5), cutoff=cutoff,
    )
    new_lineage = InputLineage(
        field="new-source", source_id="new", source_version="1",
        event_at=cutoff - timedelta(minutes=5),
        available_at=cutoff - timedelta(minutes=5), cutoff=cutoff,
    )
    plan = SimpleNamespace(
        trade_plan_id="trade-plan.v1:new",
        publication_id="ranking:new",
        opportunity_episode_id="episode:new",
        decision_revision="decision:new",
        policy_version="policy:v1",
        selected_expression_kind=ExpressionKind.CALL,
        selected_expression_identity="expression:new",
        rank_id="rank:new",
        alpha_signal_id="signal:new",
        portfolio_impact_id="impact:new",
        market_snapshot_id="snapshot:new",
        market_state_publication_id="market:new",
        input_lineage=(new_lineage,),
    )
    old = OutcomeAttribution.model_validate({
        "stable_unit_key": "trade-plan.v1:old:TACTICAL:1",
        "ticker": "QQQ",
        "trade_plan_id": "trade-plan.v1:old",
        "trade_plan_publication_id": "ranking:old",
        "opportunity_episode_id": "episode:old",
        "decision_revision": "decision:old",
        "policy_version": "policy:v1",
        "selected_expression_kind": "CALL",
        "selected_expression_identity": "expression:old",
        "decision_cutoff": cutoff,
        "evaluation_cutoff": cutoff + timedelta(days=1),
        "decision_input_lineage": (old_lineage,),
        "horizon": "TACTICAL",
        "horizon_sessions": 1,
    }).model_dump(mode="json")
    forged = OutcomeAttribution.model_validate({
        "stable_unit_key": "trade-plan.v1:new:TACTICAL:1",
        "ticker": "QQQ",
        "trade_plan_id": plan.trade_plan_id,
        "trade_plan_publication_id": "ranking:old",
        "opportunity_episode_id": "episode:old",
        "decision_revision": "decision:old",
        "policy_version": "policy:v1",
        "selected_expression_kind": "CALL",
        "selected_expression_identity": "expression:old",
        "decision_cutoff": cutoff,
        "evaluation_cutoff": cutoff + timedelta(days=1),
        "decision_input_lineage": (old_lineage,),
        "horizon": "TACTICAL",
        "horizon_sessions": 1,
    }).model_dump(mode="json")
    plan_payload = {
        "trade_plan_id": plan.trade_plan_id,
        "publication_id": plan.publication_id,
        "opportunity_episode_id": plan.opportunity_episode_id,
        "decision_revision": plan.decision_revision,
        "policy_version": plan.policy_version,
        "selected_expression_kind": plan.selected_expression_kind.value,
        "selected_expression_identity": plan.selected_expression_identity,
        "rank_id": plan.rank_id,
        "alpha_signal_id": plan.alpha_signal_id,
        "portfolio_impact_id": plan.portfolio_impact_id,
        "market_snapshot_id": plan.market_snapshot_id,
        "market_state_publication_id": plan.market_state_publication_id,
        "input_lineage": [new_lineage.model_dump(mode="json")],
    }
    repository = object.__new__(owner.TickerDecisionRepository)
    repository.runtime = None
    repository.latest = lambda _ticker: SimpleNamespace(
        model_dump=lambda mode: {"ticker": "QQQ", "trade_plan": plan_payload},
        trade_plan=plan,
        tactical=SimpleNamespace(model_dump=lambda mode: {}),
        fundamental=SimpleNamespace(model_dump=lambda mode: {}),
        expressions={},
    )
    monkeypatch.setattr(
        owner.AnalysisRepository, "publication_rows", lambda *_args, **_kwargs: [old, forged],
    )
    evaluated: list[dict[str, object]] = []
    monkeypatch.setattr(
        owner, "evaluate_ticker_policy",
        lambda rows, canonical_only=False: evaluated.extend(rows) or {"rows": list(rows)},
    )

    result = repository.learning_surface("QQQ")

    assert result["outcome_attributions"] == []
    assert evaluated == []
    assert result["strategy_learning"]["blockers"] == [
        "outcome_attribution_lineage_mismatch",
    ]
