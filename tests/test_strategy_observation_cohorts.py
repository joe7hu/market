from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.database.options_experiments import EXPERIMENT_VERSION
from investment_panel.database.strategy_learning import evaluate_comparison, forward_cohort, measured_rows


def _observation(revision, index, day, state="closed", reason=None):
    experiment = {"version": EXPERIMENT_VERSION, "paper_only": True, "run_id": f"run-{revision}-{index}"}
    if revision == 2:
        experiment.update(candidate_revision_id=2, parent_revision_id=1, proposal_id="proposal")
        kind, scope, input_key = "options-paper-experiment", "options-paper-experiment:2", "experiment"
    else:
        experiment["incumbent_revision_id"] = 1
        kind, scope, input_key = "options-radar", "options-paper-incumbent:1", "observation"
    return {
        "decision_id": f"decision-{revision}-{index}", "strategy_revision_id": revision,
        "episode_key": f"episode-{index}", "paper_order_count": 0,
        "as_of": datetime(2020, 1, 2, 15, tzinfo=UTC) + timedelta(days=day),
        "shadow_status": state, "state": "REJECTED" if state == "rejected" else "WATCH",
        "pending_entry_reason": reason or ("candidate_gate_rejected" if state == "rejected" else None),
        "entry_at": None, "entry_price": None, "exit_at": None, "exit_price": None,
        "run_id": experiment["run_id"], "run_type": kind, "scope": scope,
        "metrics": {"experiment": experiment},
        "payload": {"experiment": experiment, "ticket": {"experiment": experiment}},
        "inputs": {input_key: {key: value for key, value in experiment.items() if key != "run_id"}},
    }


def _cohort():
    observations = [_observation(revision, "initial-partial-session", -1, "pending") for revision in (1, 2)]
    baseline, proposed = [], []
    for index, day in enumerate((0, 7, 14, 21, 28, 31)):
        parent = _observation(1, index, day)
        candidate = _observation(2, index, day, "closed" if index in (0, 5) else "rejected")
        observations.extend((parent, candidate))
        baseline.append({**parent, "current_return": .1, "peak_return": .2, "max_drawdown": -.1})
        if candidate["shadow_status"] == "closed":
            proposed.append({**candidate, "current_return": .2, "peak_return": .3, "max_drawdown": -.1})
    # Later open observations do not move this pre-defined completed window.
    observations.extend(_observation(revision, "later-window", 40, "pending") for revision in (1, 2))
    return observations, baseline, proposed


def _compare(observations, baseline, proposed):
    parent, candidate, context = forward_cohort(
        observations, baseline, proposed, candidate_id=2, parent_id=1, proposal_id="proposal", span_days=30,
    )
    return evaluate_comparison(parent, candidate, minimum=2, require_span_days=30, paired=True, **context)


def test_fixed_forward_window_counts_confirmed_cash_without_inventing_fills():
    result = _compare(*_cohort())
    assert result["verdict"] == "fail"
    assert result["comparison_denominator"] == 6
    assert result["proposed"]["sample_size"] == 2
    assert result["confirmed_cash"] == {"baseline": 0, "proposed": 4}
    assert result["candidate_net_on_comparison_universe"] == pytest.approx(.4 / 6)
    assert result["baseline_net_on_comparison_universe"] == pytest.approx(.1)
    assert result["missed_winners"] == 4 / 6
    assert result["cohort"]["period_end"] == "2020-02-02"


def test_confidence_uses_the_same_complete_trade_and_cash_universe():
    observations, baseline, proposed = _cohort()
    # Selected candidate trades are all winners and beat a weak incumbent,
    # but four CASH episodes make the full-universe lower bound negative.
    baseline = [{**row, "current_return": .01} for row in baseline]
    result = _compare(observations, baseline, proposed)
    assert result["proposed"]["lower_95_expectancy"] > 0
    assert result["candidate_net_on_comparison_universe"] > result["baseline_net_on_comparison_universe"]
    assert result["comparison_lower_95"] < 0
    assert result["verdict"] == "fail"


@pytest.mark.parametrize("state,reason", [
    ("pending", None), ("entered", None), ("unmeasurable", None),
    ("unfilled", "entry_window_elapsed"), ("unfilled", "candidate_authority_changed"),
    ("rejected", "unproved_rejection"),
])
def test_unobserved_or_unfilled_candidate_remains_unknown(state, reason):
    observations, baseline, proposed = _cohort()
    item = next(row for row in observations if row["decision_id"] == "decision-2-1")
    item.update(shadow_status=state, pending_entry_reason=reason)
    result = _compare(observations, baseline, proposed)
    assert result["verdict"] == ("blocked_terminal_evidence" if state in {"unfilled", "unmeasurable"} else "collecting_data")
    assert result["comparison_denominator"] == 6 and result["unmatched_episodes"] == 1
    assert result["candidate_net_on_comparison_universe"] is None
    assert result["baseline_net_on_comparison_universe"] is None
    assert result["comparison_lower_95"] is None
    assert result["missed_winners"] is None


def test_absent_incumbent_or_forged_cash_lineage_cannot_shrink_the_union():
    observations, baseline, proposed = _cohort()
    observations = [row for row in observations if row["decision_id"] != "decision-1-1"]
    result = _compare(observations, baseline, proposed)
    assert result["comparison_denominator"] == 6 and result["unmatched_episodes"] == 1
    assert result["candidate_net_on_comparison_universe"] is None
    observations, baseline, proposed = _cohort()
    next(row for row in observations if row["decision_id"] == "decision-2-1")["metrics"] = {}
    assert _compare(observations, baseline, proposed)["verdict"] == "collecting_data"


def test_parent_paper_orders_cannot_supply_candidate_execution_measurements():
    observations, baseline, proposed = _cohort()
    for row in observations:
        if row["strategy_revision_id"] == 2:
            row["first_paper_at"] = row["as_of"]
    parent, candidate, context = forward_cohort(
        observations, baseline, proposed, candidate_id=2, parent_id=1, proposal_id="proposal", span_days=20,
        execution_grade=True,
    )
    assert parent == [] and candidate == []
    result = evaluate_comparison(parent, candidate, minimum=20, require_span_days=20, paired=True, **context)
    assert result["verdict"] == "collecting_data" and result["proposed"]["sample_size"] == 0


def test_paper_drawdown_requires_its_own_observed_marks_not_shadow_drawdown():
    at = datetime(2020, 1, 1, tzinfo=UTC)
    row = {
        "as_of": at, "paper_order_id": "paper", "paper_only": True, "paper_status": "exited", "paper_order_count": 1,
        "filled_at": at + timedelta(hours=1), "exit_at": at + timedelta(hours=2),
        "actual_fill_price": .5, "exit_price": .7, "filled_quantity": 1, "exited_quantity": 1,
        "entry_quantity": 1, "exit_quantity": 1, "contract_multiplier": 100,
        "fees": 1.3, "entry_slippage": .01, "exit_slippage": .01,
        "structure": "long_call", "current_return": 2.0, "peak_return": 3.0, "max_drawdown": -.8,
    }
    measured = measured_rows([row])[0]
    assert measured["current_return"] == pytest.approx(.374)
    assert measured["max_drawdown"] is None and measured["shadow_max_drawdown"] == -.8
    marks = {"status": "observed", "mark_count": 2, "measured_at": (at + timedelta(hours=2)).isoformat(), "max_drawdown": -.05}
    assert measured_rows([{**row, "paper_marks": marks}])[0]["max_drawdown"] == -.05
    assert measured_rows([{**row, "paper_marks": {**marks, "status": "unknown"}}])[0]["max_drawdown"] is None
    assert measured_rows([{**row, "paper_marks": {**marks, "measured_at": "9999-01-01T00:00:00+00:00"}}])[0]["max_drawdown"] is None


def test_terminal_gap_keeps_the_failed_window_despite_later_winners():
    observations, baseline, proposed = _cohort()
    next(row for row in observations if row["decision_id"] == "decision-2-1").update(
        shadow_status="unfilled", pending_entry_reason="entry_window_elapsed",
    )
    before = _compare(observations, baseline, proposed)
    for day in range(50, 170):
        for revision, source in ((1, baseline), (2, proposed)):
            row = _observation(revision, f"later-{day}", day)
            observations.append(row)
            source.append({**row, "current_return": .5, "peak_return": .5, "max_drawdown": 0})
    after = _compare(observations, baseline, proposed)
    assert before == after
    assert after["verdict"] == "blocked_terminal_evidence"
    assert after["cohort"]["terminal_unmatched_episodes"] == 1
    assert after["comparison_lower_95"] is None


def test_span_uses_the_whole_window_with_weekend_start_and_cash_boundary():
    # Friday's first observation fixes a Saturday start. Twenty weekday trades
    # finish Friday; Monday's confirmed CASH closes the 30-day comparison.
    observations = [_observation(revision, "friday-start", 1, "pending") for revision in (1, 2)]
    baseline, proposed = [], []
    days = [day for day in range(4, 30) if _observation(1, "date", day)["as_of"].weekday() < 5]
    assert len(days) == 20
    for day in [*days, 32]:
        parent = _observation(1, day, day)
        candidate = _observation(2, day, day, "rejected" if day == 32 else "closed")
        observations.extend((parent, candidate))
        baseline.append({**parent, "current_return": .01, "peak_return": .01, "max_drawdown": 0})
        if day != 32:
            proposed.append({**candidate, "current_return": .2, "peak_return": .2, "max_drawdown": 0})
    parent, candidate, context = forward_cohort(
        observations, baseline, proposed, candidate_id=2, parent_id=1, proposal_id="proposal", span_days=30,
    )
    assert (max(row["as_of"] for row in candidate) - min(row["as_of"] for row in candidate)).days == 25
    result = evaluate_comparison(parent, candidate, minimum=20, require_span_days=30, paired=True, **context)
    assert result["verdict"] == "pass" and result["observation_span_days"] == 30
    assert result["proposed"]["sample_size"] == 20 and result["comparison_denominator"] == 21
    assert evaluate_comparison(parent, candidate, minimum=21, require_span_days=30, paired=True, **context)["verdict"] != "pass"

    from investment_panel.database.strategy_learning import StrategyLearningRepository

    class Capture:
        def execute(self, _sql, parameters):
            self.parameters = parameters

    connection = Capture()
    StrategyLearningRepository._store_evaluation(connection, 2, "shadow", result, candidate)
    assert connection.parameters[2].date().isoformat() == "2020-01-04"
    assert connection.parameters[3].date().isoformat() == "2020-02-03"


def test_closed_score_rejected_trade_cannot_supply_a_qualification_return():
    observations, baseline, proposed = _cohort()
    next(row for row in observations if row["decision_id"] == "decision-2-0")["state"] = "REJECTED"
    result = _compare(observations, baseline, proposed)
    assert result["proposed"]["sample_size"] == 1
    assert result["unmatched_episodes"] == 1
    assert result["candidate_net_on_comparison_universe"] is None
    assert result["comparison_lower_95"] is None


def _paper(row, losing=False):
    at = row["as_of"] + timedelta(hours=1)
    return {
        **row, "decision_id": row["decision_id"] + "-later-ready", "as_of": at,
        "paper_order_id": row["decision_id"], "paper_only": True, "paper_status": "exited", "paper_order_count": 1,
        "filled_at": at + timedelta(minutes=1), "exit_at": at + timedelta(hours=1),
        "actual_fill_price": 1, "exit_price": .5 if losing else 1.2,
        "filled_quantity": 1, "exited_quantity": 1, "entry_quantity": 1, "exit_quantity": 1,
        "contract_multiplier": 100, "fees": 0, "entry_slippage": 0, "exit_slippage": 0,
        "structure": "long_call",
    }


@pytest.mark.parametrize("completed", [True, False])
def test_later_paper_execution_overrides_first_rejected_observation(completed):
    observations, baseline, proposed = _cohort()
    for row in observations:
        if row["shadow_status"] != "rejected":
            row["first_paper_at"] = row["as_of"] + timedelta(minutes=1)
            row["paper_order_count"] = 1
    rejected = next(row for row in observations if row["decision_id"] == "decision-2-1")
    rejected["first_paper_at"] = rejected["as_of"] + timedelta(hours=1)
    rejected["paper_order_count"] = 1

    baseline = measured_rows([_paper(row) for row in baseline])
    proposed = measured_rows([_paper(row) for row in proposed] + ([_paper(rejected, losing=True)] if completed else []))
    parent, candidate, context = forward_cohort(
        observations, baseline, proposed, candidate_id=2, parent_id=1, proposal_id="proposal",
        span_days=30, execution_grade=True,
    )
    result = evaluate_comparison(parent, candidate, minimum=2, require_span_days=30, paired=True, **context)
    assert result["comparison_denominator"] == 6
    assert result["confirmed_cash"]["proposed"] == 3
    assert result["cohort"]["period_end"] == "2020-02-02"
    if completed:
        assert result["proposed"]["sample_size"] == 3
        assert result["candidate_net_on_comparison_universe"] == pytest.approx(-.1 / 6)
        assert result["unmatched_episodes"] == 0
    else:
        assert result["proposed"]["sample_size"] == 2
        assert result["candidate_net_on_comparison_universe"] is None
        assert result["comparison_lower_95"] is None
        assert result["unmatched_episodes"] == 1
    # Execution evidence must not change the separate shadow experiment.
    assert _compare(*_cohort())["confirmed_cash"]["proposed"] == 4


@pytest.mark.parametrize("first_complete", [True, False])
def test_multiple_paper_orders_keep_episode_unknown_even_with_a_completed_winner(first_complete):
    observations, baseline, proposed = _cohort()
    for row in observations:
        if row["shadow_status"] != "rejected":
            row.update(first_paper_at=row["as_of"] + timedelta(minutes=1), paper_order_count=1)
    rejected = next(row for row in observations if row["decision_id"] == "decision-2-1")
    rejected.update(first_paper_at=rejected["as_of"] + timedelta(hours=1), paper_order_count=2)
    first = {**_paper(rejected), "paper_order_count": 2, "current_return": .9, "peak_return": .9}
    if not first_complete:
        first.update(paper_status="entered", exit_at=None, exit_price=None, exited_quantity=0, exit_quantity=None)
    second = {**_paper(rejected, losing=first_complete), "paper_order_id": "second-order",
              "decision_id": "another-decision-in-same-episode", "paper_order_count": 2}
    parent, candidate, context = forward_cohort(
        observations, measured_rows([_paper(row) for row in baseline]),
        measured_rows([_paper(row) for row in proposed] + [first, second]),
        candidate_id=2, parent_id=1, proposal_id="proposal", span_days=30, execution_grade=True,
    )
    result = evaluate_comparison(parent, candidate, minimum=2, require_span_days=30, paired=True, **context)
    assert result["comparison_denominator"] == 6 and result["unmatched_episodes"] == 1
    assert result["proposed"]["sample_size"] == 2 and result["confirmed_cash"]["proposed"] == 3
    assert result["candidate_net_on_comparison_universe"] is None and result["comparison_lower_95"] is None
    assert result["cohort"]["multiple_paper_order_episodes"] == 1
    assert result["cohort"]["multiple_paper_order_episode_keys"] == [{"strategy_revision_id": 2, "episode_key": "episode-1"}]
    assert _compare(*_cohort())["confirmed_cash"]["proposed"] == 4
