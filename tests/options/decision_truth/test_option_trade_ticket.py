from datetime import UTC, date, datetime, timedelta

import pytest

from investment_panel.core.option_trade_ticket import (
    build_option_trade_ticket,
    calibrated_cohort_ready,
    execution_policy,
    expectancy_per_max_risk,
    sizing_policy,
)
from investment_panel.core.decision import is_market_open
from investment_panel.core.risk_policy import PortfolioAssignmentPolicy, RiskPolicySnapshot
from investment_panel.database.actions import ordered_ticket_snapshot
from investment_panel.database.options_history_v3_candidates import (
    history_truth_blockers,
    non_overlapping_returns,
)
from investment_panel.database.options_publication import (
    add_contract_fields,
    assign_ranks,
    shortlist,
    summary_state,
    trade_rank_fields,
)
from investment_panel.database.options_risk_context import broker_available


NOW = datetime(2026, 7, 30, 15, 0, tzinfo=UTC)


def _leg(
    *,
    contract_id: int = 1,
    side: str = "long",
    strike: float = 100,
    bid: float = 1.9,
    ask: float = 2.0,
    seconds_old: int = 30,
    open_interest: int = 500,
) -> dict[str, object]:
    return {
        "contract_id": contract_id,
        "option_type": "call",
        "side": side,
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "bid_size": 10,
        "ask_size": 12,
        "observed_at": (NOW - timedelta(seconds=seconds_old)).isoformat(),
        "open_interest": open_interest,
        "volume": 100,
    }


def test_defined_risk_sizing_uses_sleeve_caps_and_never_defaults_to_one() -> None:
    missing = sizing_policy(
        structure="long_call",
        sleeve_capital=None,
        broker_available_capital=100_000,
        one_unit_max_loss=100,
        secured_cash=None,
    )
    assert missing["recommended_quantity"] == 0
    assert missing["blockers"] == ["options_risk_sleeve_required"]

    sized = sizing_policy(
        structure="long_call",
        sleeve_capital=100_000,
        broker_available_capital=100_000,
        one_unit_max_loss=100,
        secured_cash=None,
        open_symbol_risk=200,
        open_total_defined_risk=600,
    )
    assert sized["available_risk_budget"] == 250
    assert sized["recommended_quantity"] == 2
    assert sized["total_risk"] == 200

    nav_veto = sizing_policy(
        structure="long_call",
        sleeve_capital=1_000_000,
        broker_available_capital=100_000,
        broker_net_liquidation=100_000,
        one_unit_max_loss=100,
        secured_cash=None,
    )
    assert nav_veto["recommended_quantity"] == 0
    assert "options_risk_sleeve_exceeds_broker_nav" in nav_veto["blockers"]


def test_exchange_early_close_is_not_treated_as_regular_session() -> None:
    assert is_market_open(datetime(2026, 11, 27, 17, 59, tzinfo=UTC))
    assert not is_market_open(datetime(2026, 11, 27, 18, 1, tzinfo=UTC))
    assert is_market_open(datetime(2026, 7, 2, 18, 1, tzinfo=UTC))
    assert not is_market_open(datetime(2028, 7, 3, 18, 1, tzinfo=UTC))


def test_summary_ready_count_follows_the_ticket_not_nominal_analysis_state() -> None:
    assert summary_state({"state": "READY", "ticket": {"state": "RESEARCH"}}) == "WATCH"
    assert summary_state({"state": "READY", "ticket": {"state": "READY"}}) == "READY"


def test_cash_secured_put_sizing_respects_symbol_and_aggregate_collateral() -> None:
    sized = sizing_policy(
        structure="cash_secured_put",
        sleeve_capital=100_000,
        broker_available_capital=100_000,
        one_unit_max_loss=None,
        secured_cash=4_000,
        open_symbol_csp_collateral=1_000,
        open_total_csp_collateral=8_000,
    )
    assert sized["available_risk_budget"] == 4_000
    assert sized["recommended_quantity"] == 1
    assert sized["fully_cash_secured"] is True


@pytest.mark.parametrize(
    ("mutate", "blocker"),
    [
        (lambda legs: legs[0].update(bid=0), "positive_uncrossed_bid_ask_required"),
        (lambda legs: legs[0].update(ask=3.0), "single_leg_relative_width_over_20_percent"),
        (lambda legs: legs[0].update(quote_age_seconds=121), "quote_age_over_120_seconds"),
        (lambda legs: legs[0].update(open_interest=99), "long_leg_open_interest_below_100"),
    ],
)
def test_execution_policy_rejects_non_executable_single_leg_packages(mutate, blocker: str) -> None:
    legs = [
        {
            "side": "buy",
            "bid": 1.9,
            "ask": 2.0,
            "bid_size": 10,
            "ask_size": 10,
            "quote_time": NOW.isoformat(),
            "quote_age_seconds": 10,
            "open_interest": 100,
        }
    ]
    mutate(legs)
    result = execution_policy(legs, structure="long_call", entry_price=2.0, market_session="regular")
    assert blocker in result["blockers"]


def test_ticket_is_research_only_until_execution_sizing_thesis_and_calibration_are_complete() -> None:
    ticket = build_option_trade_ticket(
        decision_id="decision-1",
        symbol="QQQ",
        structure="long_call",
        expiration=date(2026, 8, 21),
        legs=[_leg()],
        entry_price=2.0,
        one_unit_max_loss=200,
        state="READY",
        evaluated_at=NOW,
        market_session="regular",
        sleeve_capital=None,
        broker_available_capital=100_000,
        thesis={"direction": "long", "invalidation": "Exit if QQQ closes below 540."},
        forecast={"probability_semantics": "provisional_uncalibrated", "lower_95_expected_value": 20},
    )
    assert ticket["state"] == "RESEARCH"
    assert ticket["risk"]["recommended_quantity"] == 0
    assert "options_risk_sleeve_required" in ticket["blockers"]
    assert "calibrated_probability_required" in ticket["blockers"]


def test_future_provider_timestamp_is_not_treated_as_fresh() -> None:
    ticket = build_option_trade_ticket(
        decision_id="future-quote",
        symbol="QQQ",
        structure="long_call",
        expiration=date(2026, 8, 21),
        legs=[_leg(seconds_old=-3600)],
        entry_price=2,
        one_unit_max_loss=200,
        state="READY",
        evaluated_at=NOW,
        market_session="regular",
        sleeve_capital=100_000,
        broker_available_capital=100_000,
        thesis={"invalidation": "Exit below 540."},
        forecast={"probability_semantics": "calibrated_exact_cohort"},
    )
    assert ticket["state"] == "RESEARCH"
    assert "quote_age_over_120_seconds" in ticket["blockers"]


def test_complete_debit_spread_ticket_has_exact_prices_quantity_and_time_exit() -> None:
    ticket = build_option_trade_ticket(
        decision_id="decision-2",
        symbol="QQQ",
        structure="call_debit_spread",
        expiration=date(2026, 10, 16),
        legs=[
            _leg(contract_id=1, strike=550, bid=3.9, ask=4.0),
            _leg(contract_id=2, side="short", strike=555, bid=2.0, ask=2.1),
        ],
        entry_price=2.0,
        one_unit_max_loss=200,
        state="PAPER_READY",
        evaluated_at=NOW,
        market_session="regular",
        sleeve_capital=100_000,
        broker_available_capital=100_000,
        thesis={"direction": "long", "invalidation": "Exit if QQQ closes below 540."},
        forecast={
            "probability_semantics": "calibrated_exact_cohort",
            "probability_profit": 0.58,
            "effective_sample_size": 42,
            "lower_95_expected_value": 25,
        },
    )
    assert ticket["state"] == "READY"
    assert ticket["risk"]["recommended_quantity"] == 1
    assert ticket["risk"]["total_risk"] == 200
    assert ticket["entry"]["maximum_chase_price"] == 2.1
    assert datetime.fromisoformat(ticket["entry"]["valid_until"]) == NOW + timedelta(seconds=90)
    assert ticket["exits"]["profit_price"] == 4.0
    assert ticket["exits"]["loss_price"] == 1.0
    assert ticket["exits"]["time_exit_dte"] == 7
    assert ticket["lower_confidence_expectancy_per_max_risk"] == pytest.approx(0.125)


def test_expectancy_ranking_does_not_saturate() -> None:
    assert expectancy_per_max_risk(20, 100) == pytest.approx(0.2)
    assert expectancy_per_max_risk(20, 200) == pytest.approx(0.1)
    assert expectancy_per_max_risk(None, 100) is None
    assert expectancy_per_max_risk(float("inf"), 100) is None


def test_ticket_preserves_zero_lower_confidence_expectancy() -> None:
    ticket = build_option_trade_ticket(
        decision_id="zero-expectancy",
        symbol="QQQ",
        structure="long_call",
        expiration=date(2026, 8, 21),
        legs=[_leg()],
        entry_price=2,
        one_unit_max_loss=200,
        state="WATCH",
        evaluated_at=NOW,
        market_session="regular",
        thesis={"direction": "long", "invalidation": "Exit below 540."},
        forecast={
            "probability_semantics": "calibrated_exact_cohort",
            "lower_confidence_expected_value": 0.0,
            "lower_95_expected_value": -10.0,
        },
    )
    assert ticket["forecast"]["lower_confidence_expected_value"] == 0
    assert ticket["lower_confidence_expectancy_per_max_risk"] == 0


def test_calibration_requires_predictions_lower_bound_and_cross_regime_monitoring() -> None:
    profile = {
        "sample_size": 30,
        "prediction_sample_size": 30,
        "lower_95_expectancy": 0.01,
        "brier_score": 0.2,
        "other_regime_monitoring_count": 5,
    }
    assert calibrated_cohort_ready(profile)
    assert not calibrated_cohort_ready({**profile, "prediction_sample_size": 0})
    assert not calibrated_cohort_ready({**profile, "brier_score": None})


def test_ticket_rejects_a_thesis_that_conflicts_with_the_structure() -> None:
    ticket = build_option_trade_ticket(
        decision_id="direction-conflict",
        symbol="QQQ",
        structure="long_put",
        expiration=date(2026, 8, 21),
        legs=[_leg()],
        entry_price=2,
        one_unit_max_loss=200,
        state="READY",
        evaluated_at=NOW,
        market_session="regular",
        sleeve_capital=100_000,
        broker_available_capital=100_000,
        thesis={"direction": "long", "invalidation": "Exit above 550."},
        forecast={"probability_semantics": "calibrated_exact_cohort"},
    )
    assert ticket["state"] == "RESEARCH"
    assert "thesis_direction_conflicts_with_structure" in ticket["blockers"]


def test_shortlist_preserves_zero_expectancy_over_negative_expectancy() -> None:
    rows = [
        {"ticker": "NVDA", "structure": "long_call", "decision_id": "negative", "lower_confidence_expectancy_per_max_risk": -0.1},
        {"ticker": "NVDA", "structure": "long_call", "decision_id": "zero", "lower_confidence_expectancy_per_max_risk": 0.0},
    ]
    assert shortlist(rows)[0]["decision_id"] == "zero"


def test_shortlist_falls_back_to_candidate_quality_when_expectancy_is_missing() -> None:
    rows = [
        {"ticker": "NVDA", "structure": "long_call", "decision_id": "low", "score": 10},
        {"ticker": "NVDA", "structure": "long_call", "decision_id": "high", "score": 90},
    ]
    assert shortlist(rows)[0]["decision_id"] == "high"


def test_zero_broker_capacity_is_preserved_as_a_hard_limit() -> None:
    available = broker_available(
        {
            "observed_at": NOW,
            "net_liquidation": 50_000,
            "buying_power": 0,
            "cash_balance": 10_000,
        },
        evaluated_at=NOW,
    )
    assert available == 0
    assert broker_available(
        {
            "observed_at": NOW,
            "net_liquidation": 50_000,
            "buying_power": -1,
            "cash_balance": 10_000,
        },
        evaluated_at=NOW,
    ) is None


def test_ordered_ticket_snapshot_recomputes_exposure_for_reduced_quantity() -> None:
    snapshot = ordered_ticket_snapshot(
        {
            "risk": {
                "recommended_quantity": 3,
                "total_risk": 300,
                "symbol_exposure_after_entry": 400,
                "total_options_exposure_after_entry": 700,
            }
        },
        quantity=1,
        total_risk=100,
    )
    assert snapshot["risk"]["ordered_quantity"] == 1
    assert snapshot["risk"]["total_risk"] == 100
    assert snapshot["risk"]["symbol_exposure_after_entry"] == 200
    assert snapshot["risk"]["total_options_exposure_after_entry"] == 500


def test_cash_secured_put_ticket_expresses_a_minimum_credit() -> None:
    ticket = build_option_trade_ticket(
        decision_id="decision-csp",
        symbol="NVDA",
        structure="cash_secured_put",
        expiration=date(2026, 8, 21),
        legs=[_leg(side="short")],
        entry_price=3.1,
        one_unit_max_loss=None,
        secured_cash=15_000,
        state="WATCH",
        evaluated_at=NOW,
        market_session="regular",
        sleeve_capital=500_000,
        broker_available_capital=100_000,
        thesis={"invalidation": "Exit if NVDA closes below 150."},
        forecast={"probability_semantics": "calibrated_exact_cohort"},
    )
    assert ticket["entry"]["minimum_credit"] == 3.1
    assert ticket["entry"]["maximum_chase_price"] is None


def test_calibrated_cash_secured_put_derives_a_conservative_dollar_expectancy() -> None:
    rows = [{
        "candidate_event_id": "csp-published",
        "ticker": "NVDA",
        "structure": "cash_secured_put",
        "expiration": date(2026, 8, 21),
        "contract_id": 1,
        "option_type": "put",
        "strike": 150,
        "bid": 3.0,
        "ask": 3.1,
        "bid_size": 10,
        "ask_size": 10,
        "captured_at": NOW,
        "open_interest": 500,
        "volume": 100,
        "entry_price": 3.0,
        "secured_cash": 15_000,
        "max_loss": 15_000,
        "state": "READY",
        "execution_confidence": 0.8,
        "blockers": [],
        "market_session": "regular",
        "details": {},
        "thesis_payload": {
            "direction": "long",
            "invalidation": "Exit below 145.",
            "provenance": {"option_agent_task_id": "task-1", "option_agent_run_id": "run-1"},
        },
        "thesis_revision_id": "revision-id-1",
        "thesis_revision": 2,
        "thesis_author_kind": "ai",
        "thesis_expression_id": "expression-id-1",
        "thesis_expression": {"direction": "long", "preferred_structures": ["cash_secured_put"]},
    }]
    add_contract_fields(
        rows,
        "option-professional-v3-ticket",
        3,
        options_risk_sleeve_capital=500_000,
        evaluated_at=NOW,
        risk_contexts={"NVDA": {"broker_available_capital": 100_000}},
        calibration=[{
            "structure": "cash_secured_put",
            "sample_size": 30,
            "prediction_sample_size": 30,
            "lower_95_expectancy": 0.01,
            "brier_score": 0.2,
            "other_regime_monitoring_count": 5,
        }],
    )
    assert rows[0]["ticket"]["forecast"]["lower_confidence_expected_value"] == 150
    assert rows[0]["execution_quality_score"] == 80.0
    assert "positive_lower_confidence_expectancy_required" not in rows[0]["ticket"]["blockers"]
    assert "thesis_expression_required" not in rows[0]["ticket"]["blockers"]
    assert rows[0]["ticket"]["provenance"]["thesis"]["option_agent_task_id"] == "task-1"


def test_cash_secured_put_ticket_blocks_risk_policy_version_mismatch() -> None:
    snapshot = RiskPolicySnapshot(
        policy_version="risk-policy.v2:new",
        sleeve_capital=500_000,
        cash_balance=100_000,
        buying_power=100_000,
        broker_available_capital=100_000,
        account_observed_at=NOW,
    )
    assignment = PortfolioAssignmentPolicy(
        paper_assignment_allowed=True,
        risk_policy_version="risk-policy.v2:old",
        thesis_direction="bullish",
        thesis_as_of=NOW,
        thesis_preferred_structures=("cash_secured_put",),
        account_as_of=NOW,
        account_source="postgresql",
        cash_balance=100_000,
        buying_power=100_000,
        required_cash=15_000,
        symbol_limit=25_000,
        aggregate_limit=75_000,
        evaluated_at=NOW,
    )
    ticket = build_option_trade_ticket(
        decision_id="csp-policy-version-mismatch",
        symbol="NVDA",
        structure="cash_secured_put",
        expiration=date(2026, 8, 21),
        legs=[{**_leg(side="short"), "option_type": "put"}],
        entry_price=3.1,
        one_unit_max_loss=None,
        secured_cash=15_000,
        state="READY",
        evaluated_at=NOW,
        market_session="regular",
        sleeve_capital=500_000,
        risk_policy_snapshot=snapshot,
        policy_version=snapshot.policy_version,
        assignment_policy=assignment,
        thesis={"direction": "bullish", "invalidation": "Exit below 145."},
        forecast={
            "probability_semantics": "calibrated_exact_cohort",
            "lower_95_expected_value": 100,
        },
    )
    assert ticket["state"] == "RESEARCH"
    assert "risk_policy_version_mismatch" in ticket["blockers"]
    assert ticket["resolution"]["action"] == "NO_TRADE"


def _rank_row(
    *,
    ticker: str = "NVDA",
    decision_id: str = "decision-1",
    ticket_state: str = "READY",
    eligibility: str = "ACTIONABLE",
    expectancy: float | None = 0.25,
    semantics: str = "calibrated_exact_cohort",
    lineage: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "structure": "long_call",
        "decision_id": decision_id,
        "ticket": {
            "state": ticket_state,
            "resolution": {"eligibility": eligibility},
            "lower_confidence_expectancy_per_max_risk": expectancy,
            "forecast": {
                "lower_confidence_expected_value": expectancy * 100 if expectancy is not None else None,
                "probability_semantics": semantics,
            },
            "risk": {"one_unit_max_loss": 100},
            "publication_lineage": lineage if lineage is not None else {
                "publication_scope": "options-radar",
                "analysis_run_id": "run-1",
                "analysis_cutoff": NOW.isoformat(),
                "feature_version": "feature-v1",
                "strategy_revision": 3,
                "policy_version": "risk-policy.v2:1",
                "decision_revision": decision_id,
            },
        },
    }


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"ticket_state": "RESEARCH"}, "ticket_not_ready"),
        ({"eligibility": "PENDING"}, "decision_resolution_not_actionable"),
        ({"expectancy": None}, "conservative_expectancy_not_finite"),
        ({"expectancy": float("nan")}, "conservative_expectancy_not_finite"),
        ({"expectancy": float("inf")}, "conservative_expectancy_not_finite"),
        ({"expectancy": 0.0}, "conservative_expectancy_not_positive"),
        ({"expectancy": -0.1}, "conservative_expectancy_not_positive"),
        ({"semantics": "provisional_uncalibrated"}, "probability_semantics_not_calibrated"),
        ({"lineage": {}}, "publication_lineage_incomplete"),
    ],
)
def test_trade_rank_is_fail_closed_with_exactly_one_reason(
    updates: dict[str, object], reason: str
) -> None:
    row = _rank_row(**updates)
    fields = trade_rank_fields(row)
    assert fields == {"trade_rank": None, "trade_rank_unavailable_reason": reason}


def test_trade_rank_uses_only_positive_calibrated_actionable_ticket_utility() -> None:
    rows = [
        _rank_row(ticker="BBB", decision_id="bbb", expectancy=1.0),
        _rank_row(ticker="AAA", decision_id="aaa", expectancy=1.0),
        _rank_row(ticker="CCC", decision_id="ccc", expectancy=2.0),
    ]
    assign_ranks(rows)
    assert {row["ranking_version"] for row in rows} == {"options-radar-ranking.v1"}
    assert {row["research_rank"] for row in rows} == {1, 2, 3}
    assert [(row["ticker"], row["trade_rank"]) for row in rows] == [
        ("BBB", 3), ("AAA", 2), ("CCC", 1)
    ]
    assert all(row["trade_rank_unavailable_reason"] is None for row in rows)


def test_legacy_score_fields_do_not_synthesize_trade_rank() -> None:
    fields = trade_rank_fields({
        "score": 99,
        "rank_score": 99,
        "risk_adjusted_expectancy": 100,
        "execution_quality_score": 100,
    })
    assert fields == {"trade_rank": None, "trade_rank_unavailable_reason": "ticket_not_ready"}


def test_ranking_does_not_change_ticket_sizing_or_paper_state() -> None:
    row = _rank_row()
    row["ticket"]["risk"] = {"one_unit_max_loss": 100, "recommended_quantity": 2}
    assign_ranks([row])
    assert row["ticket"]["state"] == "READY"
    assert row["ticket"]["risk"]["recommended_quantity"] == 2


def test_historical_ticket_exit_horizon_uses_evaluation_date() -> None:
    ticket = build_option_trade_ticket(
        decision_id="historical",
        symbol="QQQ",
        structure="long_call",
        expiration=date(2026, 12, 18),
        legs=[_leg()],
        entry_price=2,
        one_unit_max_loss=200,
        state="WATCH",
        evaluated_at=datetime(2024, 7, 30, 15, tzinfo=UTC),
        market_session="regular",
        thesis={"invalidation": "Exit below 400."},
        forecast={"probability_semantics": "provisional_uncalibrated"},
    )
    assert ticket["exits"]["time_exit_dte"] == 60


def test_return_horizon_matches_actual_dte_without_leap_cap() -> None:
    bars = [{"close": float(value)} for value in range(1, 601)]
    assert len(non_overlapping_returns(bars, 540)) == 1
    assert non_overlapping_returns(bars[:373], 540) == ()


def test_history_truth_rejects_stale_and_split_like_unadjusted_series() -> None:
    stale = [{"trading_date": date(2026, 6, 16), "close": 100.0}]
    assert history_truth_blockers(stale, NOW) == ["underlying_history_stale_relative_to_option_quote"]
    split_like = [
        {"trading_date": date(2026, 7, 29), "close": 100.0},
        {"trading_date": date(2026, 7, 30), "close": 50.0},
    ]
    assert history_truth_blockers(split_like, NOW) == ["unresolved_corporate_action_in_price_history"]
