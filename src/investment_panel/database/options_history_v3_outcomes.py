"""Scorecard-safe persistence for history-v3 shadow marks."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def upsert_observing_shadow_outcome(
    connection: Any,
    *,
    decision_id: str,
    shadow_trade_id: str,
    observed_through: datetime,
    current_return: float | None,
) -> None:
    """Keep an incomplete shadow mark visible but out of scorecard cohorts."""

    connection.execute(
        """
        INSERT INTO analysis.option_outcome
            (decision_id, maturity_state, observed_through, current_return, outcome_source, shadow_trade_id,
             lane, episode_key, sample_eligible, quarantine_reason, calibration_cohort)
        SELECT decision.id, 'observing', %s, %s, 'options_history_v3', %s,
               decision.lane, decision.episode_key, false,
               coalesce(decision.quarantine_reason, 'outcome_not_resolved_execution_grade'),
               decision.calibration_cohort
        FROM analysis.decision decision
        WHERE decision.id = %s
        ON CONFLICT (decision_id) DO UPDATE
        SET maturity_state = EXCLUDED.maturity_state, observed_through = EXCLUDED.observed_through,
            current_return = EXCLUDED.current_return, outcome_source = EXCLUDED.outcome_source,
            shadow_trade_id = EXCLUDED.shadow_trade_id, lane = EXCLUDED.lane,
            episode_key = EXCLUDED.episode_key, sample_eligible = false,
            quarantine_reason = EXCLUDED.quarantine_reason,
            calibration_cohort = EXCLUDED.calibration_cohort, updated_at = now()
        """,
        [observed_through, current_return, shadow_trade_id, decision_id],
    )
