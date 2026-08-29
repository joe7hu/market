from __future__ import annotations

from datetime import UTC, datetime

import pytest

from investment_panel.core.decision import (
    ExpressionKind,
    apply_opportunity_rank_safety,
    build_instrument_state_snapshot,
    build_ticker_decision,
    calculate_trade_utility,
    rank_opportunities,
    trade_expression_identity,
)


CUTOFF = datetime(2026, 8, 25, 14, tzinfo=UTC)


def _candidate(
    ticker: str,
    *,
    utility: float = 0.2,
    kind: str = "STOCK",
    evaluation_stage: str | None = "out_of_sample",
) -> dict[str, object]:
    episode = f"episode:{ticker}"
    revision = f"revision:{ticker}"
    snapshot = f"snapshot:{ticker}"
    publication = f"publication:{ticker}"
    impact = f"impact:{ticker}"
    signal = f"signal:{ticker}"
    lineage = [{"field": "quote", "source_id": "quotes", "available_at": CUTOFF}]
    expression = {
        "kind": kind,
        "ticker": ticker,
        "horizon": "FUNDAMENTAL",
        "thesis_revision": revision,
        "stance": "BULLISH",
        "status": "eligible",
        "rationale": "test expression",
    }
    expression_identity = trade_expression_identity(expression)
    return {
        "ticker": ticker,
        "opportunity_episode_id": episode,
        "decision_revision": revision,
        "policy_version": "risk-policy.test",
        "selected_expression_identity": expression_identity,
        "selected_expression_kind": kind,
        "portfolio_impact_id": impact,
        "risk_policy_version": "risk-policy.test",
        "alpha_signal_id": signal,
        "instrument_state_snapshot_id": snapshot,
        "market_snapshot_id": snapshot,
        "market_state_publication_id": publication,
        "cutoff": CUTOFF,
        "input_lineage": lineage,
        "alpha_signal": {
            "signal_id": signal,
            "ticker": ticker,
            "opportunity_episode_id": episode,
            "decision_revision": revision,
            "instrument_state_snapshot_id": snapshot,
            "target": "expected_return",
            "horizon": "FUNDAMENTAL",
            "forecast_value": 0.1,
            "cohort_id": "cohort.test",
            "calibration_state": "calibrated_exact_cohort",
            "model_version": "model.test",
            "evaluation_stage": evaluation_stage,
            "as_of": CUTOFF,
            "input_cutoff": CUTOFF,
            "input_lineage": lineage,
        },
        "portfolio_impact": {
            "impact_id": impact,
            "opportunity_episode_id": episode,
            "expression_kind": kind,
            "expression_identity": expression_identity,
            "decision_revision": revision,
            "risk_policy_version": "risk-policy.test",
            "market_snapshot_id": snapshot,
            "market_state_publication_id": publication,
            "cutoff": CUTOFF,
            "input_lineage": lineage,
            "availability": "available",
            "blockers": [],
        },
        "risk_policy_snapshot": {"policy_version": "risk-policy.test", "blockers": []},
        "expression": expression,
        "execution_feasible": True,
        "lower_confidence_expected_gross_pnl": utility * 100,
        "expected_transaction_costs": 0.0,
        "tail_risk_penalty": 0.0,
        "portfolio_overlap_penalty": 0.0,
        "diversification_benefit": 0.0,
        "capital_at_risk": 100.0,
    }


def test_trade_utility_costs_net_once() -> None:
    result = calculate_trade_utility(
        lower_confidence_expected_gross_pnl=120,
        expected_transaction_costs=20,
        lower_confidence_expected_net_pnl=100,
        tail_risk_penalty=10,
        portfolio_overlap_penalty=5,
        diversification_benefit=3,
        capital_at_risk=100,
    )

    assert result.lower_confidence_expected_net_pnl == 100
    assert result.trade_utility == pytest.approx(0.88)


def test_trade_utility_rejects_a_second_costed_net_value() -> None:
    with pytest.raises(ValueError, match="does not match"):
        calculate_trade_utility(
            lower_confidence_expected_gross_pnl=120,
            expected_transaction_costs=20,
            lower_confidence_expected_net_pnl=80,
        )


def test_rank_is_dense_when_complete_and_cash_when_unavailable() -> None:
    rows = rank_opportunities(
        [_candidate("AAA", utility=0.2), _candidate("BBB", utility=0.4)],
        evaluated_universe_complete=True,
    )

    by_ticker = {row.ticker: row for row in rows}
    assert {row.research_rank for row in rows} == {1, 2}
    assert by_ticker["BBB"].trade_rank == 1
    assert by_ticker["BBB"].trade_utility == pytest.approx(0.4)

    incomplete = rank_opportunities(
        [_candidate("AAA"), _candidate("BBB")],
        evaluated_universe_complete=False,
    )
    assert all(row.trade_rank is None for row in incomplete)
    assert all(row.trade_rank_unavailable_reason == "ranking_universe_incomplete" for row in incomplete)


@pytest.mark.parametrize(
    ("nested", "field"),
    [
        ("alpha_signal", "signal_id"),
        ("portfolio_impact", "impact_id"),
        ("risk_policy_snapshot", "policy_version"),
    ],
)
def test_rank_rejects_missing_nested_identity(nested: str, field: str) -> None:
    candidate = _candidate("AAA")
    candidate[nested].pop(field)  # type: ignore[index]

    rank = rank_opportunities([candidate], evaluated_universe_complete=True)[0]

    assert rank.trade_rank is None
    assert rank.trade_rank_unavailable_reason == "publication_lineage_mismatch"


def test_rank_rejects_missing_expression_identity() -> None:
    candidate = _candidate("AAA")
    candidate.pop("expression")

    rank = rank_opportunities([candidate], evaluated_universe_complete=True)[0]

    assert rank.trade_rank is None
    assert rank.trade_rank_unavailable_reason == "publication_lineage_mismatch"


def test_rank_rejects_missing_market_publication_identity() -> None:
    candidate = _candidate("AAA")
    candidate["portfolio_impact"].pop("market_state_publication_id")  # type: ignore[index]

    rank = rank_opportunities([candidate], evaluated_universe_complete=True)[0]

    assert rank.trade_rank is None
    assert rank.trade_rank_unavailable_reason == "publication_lineage_mismatch"


def test_cash_is_a_zero_utility_comparator() -> None:
    rows = rank_opportunities(
        [_candidate("AAA", kind="CASH")],
        evaluated_universe_complete=True,
    )

    assert rows[0].trade_rank is None
    assert rows[0].trade_rank_unavailable_reason == "cash_comparator"


@pytest.mark.parametrize("evaluation_stage", [None, "research"])
def test_trade_rank_requires_exact_cohort_out_of_sample_evidence(evaluation_stage: str | None) -> None:
    row = rank_opportunities(
        [_candidate("AAA", evaluation_stage=evaluation_stage)],
        evaluated_universe_complete=True,
    )[0]

    assert row.trade_rank is None
    assert row.trade_rank_unavailable_reason == "calibration_not_exact_out_of_sample"


@pytest.mark.parametrize("tail_risk_penalty", [20.0, 25.0])
def test_non_positive_trade_utility_selects_cash_and_cannot_order(tail_risk_penalty: float) -> None:
    candidate = _candidate("AAA")
    candidate["tail_risk_penalty"] = tail_risk_penalty
    rank = rank_opportunities([candidate], evaluated_universe_complete=True)[0]

    assert rank.trade_rank is None
    assert rank.trade_utility is not None and rank.trade_utility <= 0
    assert rank.trade_rank_unavailable_reason == "trade_utility_not_positive"
    unsafe_rank = rank.model_dump(mode="json")
    unsafe_rank.update({"trade_rank": 1, "trade_rank_unavailable_reason": None})

    decision = build_ticker_decision(
        "AAA",
        {"decision_queue": [{"symbol": "AAA", "stance": "BULLISH", "action": "BUY", "available_at": CUTOFF}]},
        as_of=CUTOFF,
    )
    safe = apply_opportunity_rank_safety(decision, unsafe_rank)
    assert safe.selected_expression is not None
    assert safe.selected_expression.kind is ExpressionKind.CASH
    assert safe.resolution is not None and safe.resolution.action == "NO_TRADE"
    assert safe.capital_action.action.value == "AVOID"


def test_instrument_snapshot_excludes_rows_newer_than_cutoff() -> None:
    snapshot = build_instrument_state_snapshot(
        "AAA",
        {
            "fundamentals": [
                {"symbol": "AAA", "available_at": "2026-08-25T13:00:00Z", "revision": "old"},
                {"symbol": "AAA", "available_at": "2026-08-25T15:00:00Z", "revision": "future"},
            ]
        },
        as_of=CUTOFF,
    )

    assert snapshot.fundamental is not None
    assert snapshot.fundamental["revision"] == "old"


def test_instrument_snapshot_requires_aware_availability_and_tie_breaks_by_identity() -> None:
    snapshot = build_instrument_state_snapshot(
        "AAA",
        {
            "fundamentals": [
                {"symbol": "AAA", "revision": "missing"},
                {"symbol": "AAA", "revision": "naive", "available_at": datetime(2026, 8, 25, 14)},
                {"symbol": "AAA", "revision": "invalid", "available_at": "not-a-timestamp"},
                {"id": "a", "symbol": "AAA", "revision": "offset", "available_at": "2026-08-25T10:00:00-04:00"},
                {"id": "b", "symbol": "AAA", "revision": "utc-tie", "available_at": "2026-08-25T14:00:00Z"},
            ],
        },
        as_of=CUTOFF,
    )

    assert snapshot.fundamental is not None
    assert snapshot.fundamental["revision"] == "utc-tie"
