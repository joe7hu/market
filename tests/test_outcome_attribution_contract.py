from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from investment_panel.core.decision import (
    OutcomeAttribution,
    PaperExecutionOutcome,
)


CUTOFF = datetime(2026, 8, 22, 14, tzinfo=UTC)
OBSERVED = CUTOFF + timedelta(days=1)


def _payload(**updates: object) -> dict[str, object]:
    evidence = {
        "kind": "STOCK",
        "source_id": "confirmed_price_bar",
        "observed_at": OBSERVED,
        "available_at": OBSERVED,
        "gross_return": 0.1,
        "cost_adjusted_return": 0.09,
        "evidence_state": "OBSERVED",
    }
    value: dict[str, object] = {
        "stable_unit_key": "plan-1:TACTICAL:1",
        "ticker": "ACME",
        "trade_plan_id": "plan-1",
        "trade_plan_publication_id": "rank-publication-1",
        "opportunity_episode_id": "episode-1",
        "decision_revision": "revision-1",
        "policy_version": "policy-1",
        "selected_expression_kind": "STOCK",
        "selected_expression_identity": "expression-1",
        "rank_id": "rank-1",
        "alpha_signal_id": "signal-1",
        "portfolio_impact_id": "impact-1",
        "market_snapshot_id": "snapshot-1",
        "market_state_publication_id": "market-publication-1",
        "decision_cutoff": CUTOFF,
        "evaluation_cutoff": OBSERVED,
        "horizon": "TACTICAL",
        "horizon_sessions": 1,
        "state": "RESOLVED",
        "observed_through": OBSERVED,
        "available_at": OBSERVED,
        "outcome_evidence": [evidence],
        "selected_evidence": evidence,
        "selected_gross_return": 0.1,
        "selected_net_return": 0.09,
        "counterfactuals": {"STOCK": evidence},
        "all_expression_counterfactuals": {"STOCK": evidence, "CASH": {"kind": "CASH", "gross_return": 0.0}},
        "evidence_state": "OBSERVED",
        "learning_metadata": {"sample": "canary"},
    }
    value.update(updates)
    return value


def test_outcome_identity_is_content_addressed_and_excludes_publication() -> None:
    first = OutcomeAttribution.model_validate(_payload())
    second = OutcomeAttribution.model_validate({**first.model_dump(mode="json"), "publication_id": "later-publication"})

    assert first.outcome_attribution_id == second.outcome_attribution_id
    assert first.stable_unit_key == "plan-1:TACTICAL:1"

    changed = OutcomeAttribution.model_validate({
        **first.model_dump(mode="json"),
        "selected_net_return": 0.08,
        "outcome_attribution_id": "",
    })
    assert changed.outcome_attribution_id != first.outcome_attribution_id


def test_outcome_rejects_future_available_evidence() -> None:
    with pytest.raises(ValueError, match="after the evaluation cutoff"):
        OutcomeAttribution.model_validate({
            **_payload(),
            "evaluation_cutoff": OBSERVED,
            "outcome_evidence": [{
                "kind": "STOCK",
                "observed_at": OBSERVED,
                "available_at": OBSERVED + timedelta(minutes=1),
                "gross_return": 0.1,
            }],
        })


def test_outcome_identity_normalizes_equivalent_timezone_offsets() -> None:
    offset = timezone(timedelta(hours=-4))
    observed = OBSERVED.astimezone(offset)
    evidence = {
        "kind": "STOCK",
        "source_id": "confirmed_price_bar",
        "observed_at": observed,
        "available_at": observed,
        "gross_return": 0.1,
        "cost_adjusted_return": 0.09,
        "evidence_state": "OBSERVED",
    }
    equivalent = OutcomeAttribution.model_validate(_payload(
        outcome_attribution_id="",
        decision_cutoff=CUTOFF.astimezone(offset),
        evaluation_cutoff=observed,
        observed_through=observed,
        available_at=observed,
        outcome_evidence=[evidence],
        selected_evidence=evidence,
        counterfactuals={"STOCK": evidence},
        all_expression_counterfactuals={"STOCK": evidence, "CASH": {"kind": "CASH", "gross_return": 0.0}},
    ))

    assert equivalent.outcome_attribution_id == OutcomeAttribution.model_validate(_payload()).outcome_attribution_id


def test_outcome_rejects_return_without_pit_provenance() -> None:
    with pytest.raises(ValueError, match="observed and available-at"):
        OutcomeAttribution.model_validate({
            **_payload(),
            "outcome_evidence": [{"kind": "STOCK", "gross_return": 0.1}],
        })


def test_sample_eligibility_requires_exact_exited_paper_execution() -> None:
    with pytest.raises(ValueError, match="resolved paper execution"):
        OutcomeAttribution.model_validate({**_payload(), "sample_eligible": True})

    paper = PaperExecutionOutcome.model_validate({
        "trade_plan_id": "plan-1",
        "status": "EXITED",
        "entry_filled_at": OBSERVED,
        "exit_at": OBSERVED,
        "entry_fill_price": 100,
        "exit_price": 110,
        "filled_quantity": 1,
        "exited_quantity": 1,
        "entry_fill_count": 1,
        "exit_fill_count": 1,
        "realized_gross_return": 0.1,
        "realized_net_return": 0.09,
        "available_at": OBSERVED,
    })
    with pytest.raises(ValueError, match="one provable paper fill"):
        OutcomeAttribution.model_validate({
            **_payload(),
            "paper_execution": paper.model_copy(update={"exit_fill_count": None}),
            "sample_eligible": True,
        })
    value = OutcomeAttribution.model_validate({
        **_payload(), "paper_execution": paper, "sample_eligible": True, "promotion_eligible": True,
    })
    assert value.sample_eligible is True
