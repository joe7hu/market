from __future__ import annotations

from datetime import UTC, datetime

from investment_panel.database.opportunity_episodes import option_episode_key
from investment_panel.database.opportunity_scorecards import _scorecard


def test_option_episode_key_deduplicates_capture_retries_within_entry_window() -> None:
    first = option_episode_key(
        lane="recovery",
        event_id="event-1",
        symbol="NVDA",
        contract_ladder_slot="call:delta-45",
        strategy="shock_reversal_call_v1",
        entry_at=datetime(2026, 8, 12, 14, 5, tzinfo=UTC),
    )
    retry = option_episode_key(
        lane="recovery",
        event_id="event-1",
        symbol="NVDA",
        contract_ladder_slot="call:delta-45",
        strategy="shock_reversal_call_v1",
        entry_at=datetime(2026, 8, 12, 14, 55, tzinfo=UTC),
    )
    next_window = option_episode_key(
        lane="recovery",
        event_id="event-1",
        symbol="NVDA",
        contract_ladder_slot="call:delta-45",
        strategy="shock_reversal_call_v1",
        entry_at=datetime(2026, 8, 12, 15, 5, tzinfo=UTC),
    )

    assert first == retry
    assert next_window != first


def test_scorecard_uses_independent_episode_denominator_and_hides_immature_ev() -> None:
    row = {
        "episode_key": "recovery:one",
        "available_at": datetime(2026, 8, 12, 14, tzinfo=UTC),
        "selection_stage": "published",
        "sample_eligible": True,
        "data_status": "ok",
        "quarantine_reason": None,
        "outcome_classification": "captured",
        "maturity_state": "closed",
        "realized_return": 2.5,
        "lower_confidence_expectancy": 0.5,
        "entry_fill_at": None,
        "exit_fill_at": None,
    }
    repeated = {**row, "available_at": datetime(2026, 8, 12, 14, 30, tzinfo=UTC)}

    result = _scorecard(
        lane="recovery",
        as_of=datetime(2026, 8, 12, 20, tzinfo=UTC),
        window_days=120,
        raw_observation_count=2,
        episodes=[row, repeated],
    )

    assert result["raw_observation_count"] == 2
    assert result["independent_episode_count"] == 1
    assert result["resolved_independent_episode_count"] == 1
    assert result["expectancy"] is None
    assert result["lower_95_expectancy"] is None
    assert result["status"] == "COLLECTING"
    assert "need_29_more_resolved_independent_episodes" in result["gaps"]
