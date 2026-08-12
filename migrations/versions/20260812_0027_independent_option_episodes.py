"""Track independent option episodes and stock decision outcomes.

Revision ID: 20260812_0027
Revises: 20260812_0026
"""

from __future__ import annotations

from alembic import op


revision = "20260812_0027"
down_revision = "20260812_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE analysis.decision
          ADD COLUMN IF NOT EXISTS lane TEXT,
          ADD COLUMN IF NOT EXISTS episode_key TEXT,
          ADD COLUMN IF NOT EXISTS sample_eligible BOOLEAN NOT NULL DEFAULT false,
          ADD COLUMN IF NOT EXISTS quarantine_reason TEXT,
          ADD COLUMN IF NOT EXISTS calibration_cohort TEXT;

        UPDATE analysis.decision decision
        SET lane = CASE WHEN instrument.symbol = 'QQQ' THEN 'qqq' ELSE 'radar' END,
            episode_key = concat_ws(
                ':',
                CASE WHEN instrument.symbol = 'QQQ' THEN 'qqq' ELSE 'radar' END,
                instrument.symbol,
                coalesce((SELECT contract_id::text FROM analysis.option_decision WHERE decision_id = decision.id), decision.decision_key),
                coalesce((SELECT structure FROM analysis.option_decision WHERE decision_id = decision.id), 'unknown'),
                to_char(date_trunc('hour', decision.as_of AT TIME ZONE 'America/New_York'), 'YYYYMMDDHH24')
            ),
            sample_eligible = decision.kind = 'option'
                              AND coalesce(decision.quality_status, 'ok') NOT IN ('invalid', 'lookahead_blocked'),
            quarantine_reason = CASE
                WHEN decision.kind <> 'option' THEN 'not_option_decision'
                WHEN coalesce(decision.quality_status, 'ok') IN ('invalid', 'lookahead_blocked')
                    THEN coalesce(decision.quality_status, 'invalid')
                ELSE NULL
            END,
            calibration_cohort = (SELECT feature_versions->>'option' FROM analysis.run WHERE id = decision.run_id)
        FROM catalog.instrument instrument
        WHERE decision.instrument_id = instrument.id
          AND decision.lane IS NULL;

        CREATE INDEX IF NOT EXISTS ix_decision_lane_episode_asof
          ON analysis.decision (lane, episode_key, as_of DESC)
          WHERE kind = 'option';

        ALTER TABLE analysis.option_outcome
          ADD COLUMN IF NOT EXISTS lane TEXT,
          ADD COLUMN IF NOT EXISTS episode_key TEXT,
          ADD COLUMN IF NOT EXISTS sample_eligible BOOLEAN NOT NULL DEFAULT false,
          ADD COLUMN IF NOT EXISTS quarantine_reason TEXT,
          ADD COLUMN IF NOT EXISTS calibration_cohort TEXT;

        UPDATE analysis.option_outcome outcome
        SET lane = decision.lane,
            episode_key = decision.episode_key,
            sample_eligible = coalesce(decision.sample_eligible, false)
                              AND outcome.promotion_eligible IS TRUE
                              AND outcome.outcome_classification NOT IN ('legacy_non_executable', 'unmeasurable'),
            quarantine_reason = CASE
                WHEN NOT coalesce(decision.sample_eligible, false)
                    THEN coalesce(decision.quarantine_reason, 'decision_ineligible')
                WHEN outcome.promotion_eligible IS NOT TRUE THEN 'promotion_ineligible'
                WHEN outcome.outcome_classification IN ('legacy_non_executable', 'unmeasurable')
                    THEN outcome.outcome_classification
                ELSE NULL
            END,
            calibration_cohort = coalesce(decision.calibration_cohort, outcome.objective_version)
        FROM analysis.decision decision
        WHERE decision.id = outcome.decision_id;

        CREATE INDEX IF NOT EXISTS ix_option_outcome_lane_episode_eligible
          ON analysis.option_outcome (lane, episode_key, sample_eligible, observed_through DESC);

        ALTER TABLE analysis.option_opportunity_observation
          ADD COLUMN IF NOT EXISTS lane TEXT NOT NULL DEFAULT 'recovery',
          ADD COLUMN IF NOT EXISTS episode_key TEXT,
          ADD COLUMN IF NOT EXISTS sample_eligible BOOLEAN NOT NULL DEFAULT false,
          ADD COLUMN IF NOT EXISTS quarantine_reason TEXT,
          ADD COLUMN IF NOT EXISTS calibration_cohort TEXT;

        UPDATE analysis.option_opportunity_observation observation
        SET episode_key = concat_ws(
                ':',
                'recovery',
                event.id::text,
                instrument.symbol,
                coalesce(
                    (SELECT ladder_slot_key FROM analysis.option_event_contract
                     WHERE id = observation.event_contract_id),
                    observation.contract_id::text
                ),
                observation.strategy_key,
                to_char(date_trunc('hour', observation.available_at AT TIME ZONE 'America/New_York'), 'YYYYMMDDHH24')
            ),
            sample_eligible = observation.data_status = 'ok'
                              AND observation.outcome_classification <> 'unmeasurable',
            quarantine_reason = CASE
                WHEN observation.data_status <> 'ok' THEN observation.data_status
                WHEN observation.outcome_classification = 'unmeasurable'
                    THEN coalesce(observation.miss_reason, 'unmeasurable')
                ELSE NULL
            END,
            calibration_cohort = coalesce(observation.cohort_id::text, observation.objective_version)
        FROM analysis.option_event event
        JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
        WHERE observation.event_id = event.id
          AND observation.episode_key IS NULL;

        ALTER TABLE analysis.option_opportunity_observation
          ALTER COLUMN episode_key SET NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_option_opportunity_lane_episode
          ON analysis.option_opportunity_observation (lane, episode_key, available_at DESC);

        CREATE TABLE IF NOT EXISTS analysis.symbol_decision_outcome (
            decision_id UUID PRIMARY KEY REFERENCES analysis.decision(id) ON DELETE CASCADE,
            instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(id) ON DELETE RESTRICT,
            as_of TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL,
            outcome_version TEXT NOT NULL DEFAULT 'equity-v1',
            state TEXT NOT NULL DEFAULT 'observing'
                CHECK (state IN ('observing', 'resolved', 'quarantined')),
            return_1d DOUBLE PRECISION,
            return_5d DOUBLE PRECISION,
            return_20d DOUBLE PRECISION,
            spy_adjusted_return_1d DOUBLE PRECISION,
            spy_adjusted_return_5d DOUBLE PRECISION,
            spy_adjusted_return_20d DOUBLE PRECISION,
            sector_adjusted_return_1d DOUBLE PRECISION,
            sector_adjusted_return_5d DOUBLE PRECISION,
            sector_adjusted_return_20d DOUBLE PRECISION,
            mae DOUBLE PRECISION,
            mfe DOUBLE PRECISION,
            max_drawdown DOUBLE PRECISION,
            thesis_invalidated_at TIMESTAMPTZ,
            sample_eligible BOOLEAN NOT NULL DEFAULT false,
            quarantine_reason TEXT,
            measured_through TIMESTAMPTZ,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_symbol_decision_outcome_state_measured
          ON analysis.symbol_decision_outcome (state, sample_eligible, measured_through DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS analysis.ix_symbol_decision_outcome_state_measured")
    op.execute("DROP TABLE IF EXISTS analysis.symbol_decision_outcome")
    op.execute("DROP INDEX IF EXISTS analysis.ix_option_opportunity_lane_episode")
    op.execute("ALTER TABLE analysis.option_opportunity_observation DROP COLUMN IF EXISTS calibration_cohort")
    op.execute("ALTER TABLE analysis.option_opportunity_observation DROP COLUMN IF EXISTS quarantine_reason")
    op.execute("ALTER TABLE analysis.option_opportunity_observation DROP COLUMN IF EXISTS sample_eligible")
    op.execute("ALTER TABLE analysis.option_opportunity_observation DROP COLUMN IF EXISTS episode_key")
    op.execute("ALTER TABLE analysis.option_opportunity_observation DROP COLUMN IF EXISTS lane")
    op.execute("DROP INDEX IF EXISTS analysis.ix_option_outcome_lane_episode_eligible")
    op.execute("ALTER TABLE analysis.option_outcome DROP COLUMN IF EXISTS calibration_cohort")
    op.execute("ALTER TABLE analysis.option_outcome DROP COLUMN IF EXISTS quarantine_reason")
    op.execute("ALTER TABLE analysis.option_outcome DROP COLUMN IF EXISTS sample_eligible")
    op.execute("ALTER TABLE analysis.option_outcome DROP COLUMN IF EXISTS episode_key")
    op.execute("ALTER TABLE analysis.option_outcome DROP COLUMN IF EXISTS lane")
    op.execute("DROP INDEX IF EXISTS analysis.ix_decision_lane_episode_asof")
    op.execute("ALTER TABLE analysis.decision DROP COLUMN IF EXISTS calibration_cohort")
    op.execute("ALTER TABLE analysis.decision DROP COLUMN IF EXISTS quarantine_reason")
    op.execute("ALTER TABLE analysis.decision DROP COLUMN IF EXISTS sample_eligible")
    op.execute("ALTER TABLE analysis.decision DROP COLUMN IF EXISTS episode_key")
    op.execute("ALTER TABLE analysis.decision DROP COLUMN IF EXISTS lane")
