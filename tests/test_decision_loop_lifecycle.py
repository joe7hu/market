from datetime import UTC, datetime
from dataclasses import replace

import pytest

from conftest import typed_config
from investment_panel.core.job_policy import scheduler_intervals
from investment_panel.infrastructure.postgres.experiment_events import observation_lifecycle


def test_generation_pause_does_not_stop_existing_forecast_resolution(monkeypatch):
    config = typed_config()
    config = replace(config, agents=replace(config.agents, thesis_monitor=replace(config.agents.thesis_monitor, continuous_enabled=False)))
    monkeypatch.delenv("MARKET_CONTINUOUS_ADVISOR_REPLAY_SECONDS", raising=False)
    intervals = scheduler_intervals(config)
    assert "run_continuous_advisor" not in intervals
    assert "run_continuous_advisor_replay" in intervals
    monkeypatch.setenv("MARKET_CONTINUOUS_ADVISOR_REPLAY_SECONDS", "0")
    assert "run_continuous_advisor_replay" not in scheduler_intervals(config)


def test_closed_session_marks_are_not_stale_because_of_weekend():
    quote = datetime(2026, 9, 18, 20, tzinfo=UTC)
    row = {"status": "entered", "quote_observed_at": quote, "entry_price": .5, "net_return": .1, "required_next_action": "Add a thesis"}
    result = observation_lifecycle(row, now=datetime(2026, 9, 20, 16, tzinfo=UTC))
    assert result["mark_status"] == "last_session"
    assert result["net_pnl"] == 5
    assert result["admission_next_action"] == "Add a thesis"
    assert "No new entry" in result["required_next_action"]
    assert "Add a thesis" not in result["required_next_action"]
    assert observation_lifecycle(row, now=datetime(2026, 9, 21, 13, 36, tzinfo=UTC))["mark_status"] == "overdue"


@pytest.mark.parametrize("state", ["pending", "rejected", "unfilled", "unmeasurable"])
def test_nonpositions_never_inherit_return_or_pnl(state):
    result = observation_lifecycle({"status": state, "entry_price": .5, "net_return": .1}, now=datetime.now(UTC))
    assert result["net_pnl"] is None
    assert result["net_return"] is None


def test_future_and_unknown_mark_clocks_cannot_claim_current_valuation():
    now = datetime(2026, 9, 21, 14, tzinfo=UTC)
    for clock in [None, "bad", "2026-09-22T14:00:00Z", "2026-09-21T14:00:00"]:
        result = observation_lifecycle({"status": "entered", "quote_observed_at": clock}, now=now)
        assert result["mark_status"] == "overdue"
