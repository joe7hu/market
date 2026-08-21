from __future__ import annotations

from datetime import UTC, datetime

from investment_panel.database.opportunity_episodes import (
    SCORECARD_TRUTH_VERSION,
    option_episode_key,
)
from investment_panel.database.opportunity_scorecards import OpportunityScorecardRepository, scorecard_payload
from investment_panel.database.runtime import DatabaseRuntime


def test_scorecard_repository_returns_invalid_rebuilding_state_for_empty_cohort(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        result = OpportunityScorecardRepository(runtime).scorecard(lane="radar")
    finally:
        runtime.close()

    assert result["status"] == "COLLECTING"
    assert result["automatic_strategy_promotion"]["eligible"] is False


def test_option_episode_key_deduplicates_contracts_and_capture_retries_for_one_hypothesis() -> None:
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
    alternate_contract = option_episode_key(
        lane="recovery",
        event_id="event-1",
        symbol="NVDA",
        contract_ladder_slot="call:delta-30",
        strategy="shock_reversal_call_v1",
        entry_at=datetime(2026, 8, 12, 15, 5, tzinfo=UTC),
    )
    next_session = option_episode_key(
        lane="recovery",
        event_id="event-1",
        symbol="NVDA",
        contract_ladder_slot="call:delta-45",
        strategy="shock_reversal_call_v1",
        entry_at=datetime(2026, 8, 13, 14, 5, tzinfo=UTC),
    )

    assert first == retry
    assert alternate_contract == first
    assert next_session != first


def test_scorecard_uses_independent_episode_denominator_and_hides_immature_ev() -> None:
    row = {
        "episode_key": "recovery:one",
        "available_at": datetime(2026, 8, 12, 14, tzinfo=UTC),
        "selection_stage": "published",
        "sample_eligible": True,
        "calibration_cohort": f"{SCORECARD_TRUTH_VERSION}:test",
        "data_status": "ok",
        "quarantine_reason": None,
        "outcome_classification": "captured",
        "maturity_state": "closed",
        "realized_return": 2.5,
        "probability_profit": 0.5,
        "entry_fill_at": None,
        "exit_fill_at": None,
    }
    repeated = {**row, "available_at": datetime(2026, 8, 12, 14, 30, tzinfo=UTC)}

    result = scorecard_payload(
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
    assert result["funnel"] == {
        "observed": 2,
        "independent": 1,
        "published": 1,
        "ticketed": 0,
        "filled": 0,
        "closed": 0,
    }


def test_scorecard_quarantines_legacy_rows_and_does_not_offer_promotion() -> None:
    result = scorecard_payload(
        lane="radar",
        as_of=datetime(2026, 8, 12, 20, tzinfo=UTC),
        window_days=120,
        raw_observation_count=1,
        episodes=[{
            "episode_key": "radar:legacy",
            "available_at": datetime(2026, 8, 12, 14, tzinfo=UTC),
            "selection_stage": "published",
            "sample_eligible": True,
            "outcome_classification": "captured",
            "maturity_state": "closed",
            "realized_return": 2.0,
            "probability_profit": 0.99,
        }],
    )

    assert result["status"] == "INVALID"
    assert result["display_status"] == "INVALID / REBUILDING"
    assert result["independent_episode_count"] == 0
    assert result["quarantined_independent_episode_count"] == 1
    assert result["automatic_strategy_promotion"] == {
        "enabled": False,
        "eligible": False,
        "reason": "scorecard_invalid_or_rebuilding",
    }


def test_scorecard_brier_uses_probability_profit_not_ev() -> None:
    rows = [{
        "episode_key": f"radar:{index}",
        "available_at": datetime(2026, 7, 1 + index, 14, tzinfo=UTC),
        "selection_stage": "published",
        "sample_eligible": True,
        "calibration_cohort": f"{SCORECARD_TRUTH_VERSION}:test",
        "data_status": "ok",
        "quarantine_reason": None,
        "outcome_classification": "captured",
        "maturity_state": "closed",
        "realized_return": -0.5,
        "probability_profit": 0.0,
        # Deliberately different from P(profit).  It must not affect Brier.
        "lower_confidence_expectancy": 1.0,
        "entry_fill_at": None,
        "exit_fill_at": None,
    } for index in range(30)]

    result = scorecard_payload(
        lane="radar",
        as_of=datetime(2026, 8, 12, 20, tzinfo=UTC),
        window_days=120,
        raw_observation_count=len(rows),
        episodes=rows,
    )

    assert result["calibration"]["metric"] == "brier_score_probability_profit"
    assert result["calibration"]["value"] == 0.0
