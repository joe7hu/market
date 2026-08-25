from __future__ import annotations

from datetime import UTC, datetime

import pytest

from investment_panel.core.decision import (
    build_instrument_state_snapshot,
    calculate_trade_utility,
    rank_opportunities,
)


CUTOFF = datetime(2026, 8, 25, 14, tzinfo=UTC)


def _candidate(ticker: str, *, utility: float = 0.2, kind: str = "STOCK") -> dict[str, object]:
    episode = f"episode:{ticker}"
    revision = f"revision:{ticker}"
    snapshot = f"snapshot:{ticker}"
    publication = f"publication:{ticker}"
    impact = f"impact:{ticker}"
    signal = f"signal:{ticker}"
    lineage = [{"field": "quote", "source_id": "quotes", "available_at": CUTOFF}]
    return {
        "ticker": ticker,
        "opportunity_episode_id": episode,
        "decision_revision": revision,
        "policy_version": "risk-policy.test",
        "selected_expression_identity": f"{kind}:{ticker}",
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
        },
        "portfolio_impact": {
            "opportunity_episode_id": episode,
            "decision_revision": revision,
            "market_snapshot_id": snapshot,
            "market_state_publication_id": publication,
            "availability": "available",
            "blockers": [],
        },
        "risk_policy_snapshot": {"policy_version": "risk-policy.test", "blockers": []},
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


def test_cash_is_a_zero_utility_comparator() -> None:
    rows = rank_opportunities(
        [_candidate("AAA", kind="CASH")],
        evaluated_universe_complete=True,
    )

    assert rows[0].trade_rank is None
    assert rows[0].trade_rank_unavailable_reason == "cash_comparator"


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
