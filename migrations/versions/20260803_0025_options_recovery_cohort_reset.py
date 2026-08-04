"""Reset options recovery into a quarantined legacy cohort and v2 canary.

Revision ID: 20260803_0025
Revises: 20260803_0024
"""

from __future__ import annotations

from alembic import op


revision = "20260803_0025"
down_revision = "20260803_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A reset must never convert a staged or live recovery position into an
    # audit row.  This deliberately aborts before touching a single event.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM app.paper_order
            WHERE event_id IS NOT NULL
              AND status IN ('staged', 'open', 'entered', 'partial_exited')
          ) THEN
            RAISE EXCEPTION
              'options recovery cohort reset refused: staged or open recovery paper order exists';
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE analysis.option_recovery_cohort (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            objective_version TEXT NOT NULL,
            code_version TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL DEFAULT 'collecting',
            required_qualified_dates INTEGER NOT NULL DEFAULT 5,
            qualified_at TIMESTAMPTZ,
            blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_option_recovery_cohort_status
              CHECK (status IN ('collecting', 'qualified', 'retired')),
            CONSTRAINT ck_option_recovery_cohort_required_dates
              CHECK (required_qualified_dates > 0),
            UNIQUE (objective_version, code_version)
        );
        CREATE UNIQUE INDEX uq_option_recovery_current_cohort_objective
          ON analysis.option_recovery_cohort (objective_version)
          WHERE status IN ('collecting', 'qualified');

        ALTER TABLE analysis.option_event
          ADD COLUMN cohort_id UUID REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT,
          ADD COLUMN data_quality_status TEXT NOT NULL DEFAULT 'valid',
          ADD COLUMN trigger_reason TEXT,
          ADD COLUMN quote_age_minutes DOUBLE PRECISION,
          ADD COLUMN reference_trading_date DATE,
          ADD COLUMN reference_source_id TEXT,
          ADD COLUMN reference_available_at TIMESTAMPTZ,
          ADD COLUMN invalidated_at TIMESTAMPTZ,
          ADD COLUMN invalidation_reason TEXT,
          ADD COLUMN priority_components JSONB NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN capacity_defer_reason TEXT;
        ALTER TABLE analysis.option_event
          DROP CONSTRAINT IF EXISTS ck_option_event_status;
        ALTER TABLE analysis.option_event
          ADD CONSTRAINT ck_option_event_status
            CHECK (status IN ('active', 'deferred_capacity', 'closed', 'invalidated'));
        ALTER TABLE analysis.option_event
          ADD CONSTRAINT ck_option_event_data_quality_status
            CHECK (data_quality_status IN (
              'valid', 'invalid_reference_bar', 'stale_quote',
              'missing_reference', 'lookahead_blocked', 'provider_unconfirmed'
            ));

        ALTER TABLE analysis.option_event_contract
          ADD COLUMN ladder_slot_key TEXT,
          ADD COLUMN retired_reason TEXT;
        ALTER TABLE analysis.option_event_capture
          ADD COLUMN canonical_continuity_pct DOUBLE PRECISION,
          ADD COLUMN original_continuity_pct DOUBLE PRECISION;

        ALTER TABLE analysis.option_event_signal
          ADD COLUMN cohort_id UUID REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT;
        ALTER TABLE analysis.option_opportunity_observation
          ADD COLUMN cohort_id UUID REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT;
        ALTER TABLE analysis.option_event_agent_batch
          ADD COLUMN cohort_id UUID REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT;
        ALTER TABLE app.paper_order
          ADD COLUMN cohort_id UUID REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT;

        ALTER TABLE analysis.option_opportunity_observation
          DROP CONSTRAINT IF EXISTS ck_option_opportunity_data_status;
        ALTER TABLE analysis.option_opportunity_observation
          ADD CONSTRAINT ck_option_opportunity_data_status CHECK (data_status IN (
            'ok', 'stale_quote', 'continuity_missing', 'lookahead_blocked',
            'invalid_event_reference'
          ));

        CREATE TABLE analysis.option_event_detector_run (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            cohort_id UUID NOT NULL REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT,
            scheduled_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ,
            expected_symbols INTEGER NOT NULL DEFAULT 0,
            received_symbols INTEGER NOT NULL DEFAULT 0,
            fresh_symbols INTEGER NOT NULL DEFAULT 0,
            quote_age_p95_minutes DOUBLE PRECISION,
            provider_run_id UUID REFERENCES ingest.run(id) ON DELETE SET NULL,
            status TEXT NOT NULL,
            failure_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_option_event_detector_run_status
              CHECK (status IN ('succeeded', 'failed', 'skipped')),
            CONSTRAINT ck_option_event_detector_run_counts
              CHECK (expected_symbols >= 0 AND received_symbols >= 0 AND fresh_symbols >= 0),
            UNIQUE (cohort_id, scheduled_at)
        );
        CREATE INDEX ix_option_event_detector_run_cohort_time
          ON analysis.option_event_detector_run (cohort_id, scheduled_at DESC);

        CREATE TABLE analysis.option_recovery_event_session_quality (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            cohort_id UUID NOT NULL REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT,
            event_id UUID NOT NULL REFERENCES analysis.option_event(id) ON DELETE CASCADE,
            trading_date DATE NOT NULL,
            scheduled_slots INTEGER NOT NULL DEFAULT 0,
            usable_slots INTEGER NOT NULL DEFAULT 0,
            complete_slots INTEGER NOT NULL DEFAULT 0,
            contract_completeness DOUBLE PRECISION,
            canonical_continuity DOUBLE PRECISION,
            original_continuity DOUBLE PRECISION,
            capture_p95_latency_minutes DOUBLE PRECISION,
            data_defects JSONB NOT NULL DEFAULT '[]'::jsonb,
            qualification_result BOOLEAN NOT NULL DEFAULT false,
            qualification_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
            computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (event_id, trading_date)
        );
        CREATE INDEX ix_option_recovery_event_session_cohort_date
          ON analysis.option_recovery_event_session_quality (cohort_id, trading_date DESC);

        CREATE TABLE analysis.option_recovery_program_session (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            cohort_id UUID NOT NULL REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT,
            trading_date DATE NOT NULL,
            active_event_count INTEGER NOT NULL DEFAULT 0,
            detector_scheduled_runs INTEGER NOT NULL DEFAULT 0,
            detector_succeeded_runs INTEGER NOT NULL DEFAULT 0,
            provider_expected_symbols INTEGER NOT NULL DEFAULT 0,
            provider_received_symbols INTEGER NOT NULL DEFAULT 0,
            fresh_event_trigger_quotes INTEGER NOT NULL DEFAULT 0,
            quote_age_p95_minutes DOUBLE PRECISION,
            event_scheduled_slots INTEGER NOT NULL DEFAULT 0,
            event_usable_slots INTEGER NOT NULL DEFAULT 0,
            contract_completeness DOUBLE PRECISION,
            canonical_continuity DOUBLE PRECISION,
            original_continuity DOUBLE PRECISION,
            capture_p95_latency_minutes DOUBLE PRECISION,
            critical_defects JSONB NOT NULL DEFAULT '[]'::jsonb,
            qualification_result BOOLEAN NOT NULL DEFAULT false,
            qualification_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
            policy_version TEXT NOT NULL,
            computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (cohort_id, trading_date)
        );
        CREATE INDEX ix_option_recovery_program_session_cohort_date
          ON analysis.option_recovery_program_session (cohort_id, trading_date DESC);
        """
    )
    # Preserve every pre-reset record behind an explicit retired audit cohort.
    # No historical event may leak into the v2 decision, learning, or promotion
    # paths after this migration.
    op.execute(
        """
        WITH legacy AS (
          INSERT INTO analysis.option_recovery_cohort
              (objective_version, code_version, started_at, status, blockers)
          VALUES (
              'short_horizon_convex_v1', 'options-recovery-v4-pre-reset',
              COALESCE((SELECT min(started_at) FROM analysis.option_event), now()),
              'retired',
              '["invalid_reference_bar", "legacy_cohort_quarantined"]'::jsonb
          )
          RETURNING id
        )
        UPDATE analysis.option_event event
        SET cohort_id = legacy.id,
            status = 'invalidated',
            data_quality_status = 'invalid_reference_bar',
            close_reason = 'invalid_reference_bar',
            invalidated_at = now(),
            invalidation_reason = 'invalid_reference_bar',
            updated_at = now()
        FROM legacy;

        UPDATE analysis.option_event_contract
        SET ladder_slot_key = 'legacy:' || id::text
        WHERE ladder_slot_key IS NULL;
        ALTER TABLE analysis.option_event_contract
          ALTER COLUMN ladder_slot_key SET NOT NULL;
        CREATE UNIQUE INDEX uq_option_event_active_ladder_slot
          ON analysis.option_event_contract (event_id, ladder_slot_key)
          WHERE retired_at IS NULL;

        UPDATE analysis.option_event_capture
        SET original_continuity_pct = continuity_pct
        WHERE original_continuity_pct IS NULL;

        UPDATE analysis.option_event_signal signal
        SET cohort_id = event.cohort_id,
            status = 'invalidated',
            gate_result = signal.gate_result || '{"invalidated_reason":"invalid_reference_bar"}'::jsonb,
            updated_at = now()
        FROM analysis.option_event event
        WHERE event.id = signal.event_id;
        ALTER TABLE analysis.option_event_signal
          ALTER COLUMN cohort_id SET NOT NULL;

        UPDATE analysis.option_opportunity_observation observation
        SET cohort_id = event.cohort_id,
            data_status = 'invalid_event_reference',
            outcome_classification = 'unmeasurable',
            miss_reason = 'unmeasurable',
            measured_through = now(),
            updated_at = now()
        FROM analysis.option_event event
        WHERE event.id = observation.event_id;
        ALTER TABLE analysis.option_opportunity_observation
          ALTER COLUMN cohort_id SET NOT NULL;

        UPDATE analysis.option_event_agent_batch batch
        SET cohort_id = event.cohort_id
        FROM analysis.option_event event
        WHERE event.id = batch.event_id;
        ALTER TABLE analysis.option_event_agent_batch
          ALTER COLUMN cohort_id SET NOT NULL;

        UPDATE app.paper_order paper
        SET cohort_id = event.cohort_id
        FROM analysis.option_event event
        WHERE event.id = paper.event_id;

        UPDATE app.option_history_policy
        SET requested_state = 'off', effective_state = 'disabled', paused_at = now(),
            reason = 'legacy recovery cohort invalid_reference_bar',
            updated_at = now(), lock_version = lock_version + 1
        WHERE profile = 'event_strip';

        INSERT INTO analysis.option_recovery_cohort
            (objective_version, code_version, started_at, status, required_qualified_dates, blockers)
        VALUES (
            'short_horizon_convex_v2', 'options-recovery-v5', now(), 'collecting', 5,
            '["five_qualified_forward_dates_required", "recovery_paper_actions_disabled"]'::jsonb
        );

        ALTER TABLE analysis.option_event
          ALTER COLUMN cohort_id SET NOT NULL;
        CREATE OR REPLACE FUNCTION analysis.assign_current_option_recovery_cohort()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.cohort_id IS NULL THEN
            SELECT id INTO NEW.cohort_id
            FROM analysis.option_recovery_cohort
            WHERE objective_version = 'short_horizon_convex_v2'
              AND status IN ('collecting', 'qualified')
            ORDER BY started_at DESC LIMIT 1;
          END IF;
          IF NEW.cohort_id IS NULL THEN
            RAISE EXCEPTION 'current options recovery cohort is required';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM analysis.option_recovery_cohort
            WHERE id = NEW.cohort_id
              AND objective_version = 'short_horizon_convex_v2'
              AND status IN ('collecting', 'qualified')
          ) THEN
            RAISE EXCEPTION 'new options recovery events must use the current v2 cohort';
          END IF;
          NEW.objective_version := 'short_horizon_convex_v2';
          RETURN NEW;
        END $$;
        CREATE TRIGGER tr_option_event_assign_current_cohort
          BEFORE INSERT ON analysis.option_event
          FOR EACH ROW EXECUTE FUNCTION analysis.assign_current_option_recovery_cohort();
        CREATE INDEX ix_option_event_cohort_status_detected
          ON analysis.option_event (cohort_id, status, detected_at DESC);
        CREATE INDEX ix_option_event_signal_cohort_status
          ON analysis.option_event_signal (cohort_id, status, available_at DESC);
        CREATE INDEX ix_option_opportunity_observation_cohort_measurement
          ON analysis.option_opportunity_observation (cohort_id, strategy_key, outcome_classification, measured_through DESC);
        CREATE INDEX ix_option_event_agent_batch_cohort_queue
          ON analysis.option_event_agent_batch (cohort_id, status, created_at);
        CREATE INDEX ix_recovery_paper_order_cohort
          ON app.paper_order (cohort_id, status, created_at DESC)
          WHERE cohort_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    # A downgrade is only a schema rollback for development databases.  Restore
    # the old vocabulary before dropping its supporting audit columns.
    op.execute("DROP TRIGGER IF EXISTS tr_option_event_assign_current_cohort ON analysis.option_event")
    op.execute("DROP FUNCTION IF EXISTS analysis.assign_current_option_recovery_cohort()")
    op.execute("UPDATE analysis.option_event SET status = 'closed' WHERE status = 'invalidated'")
    op.execute(
        """
        UPDATE analysis.option_opportunity_observation
        SET data_status = 'continuity_missing'
        WHERE data_status = 'invalid_event_reference';
        ALTER TABLE analysis.option_opportunity_observation
          DROP CONSTRAINT IF EXISTS ck_option_opportunity_data_status;
        ALTER TABLE analysis.option_opportunity_observation
          ADD CONSTRAINT ck_option_opportunity_data_status CHECK (data_status IN (
            'ok', 'stale_quote', 'continuity_missing', 'lookahead_blocked'
          ));
        """
    )
    for index in (
        "ix_recovery_paper_order_cohort",
        "ix_option_event_agent_batch_cohort_queue",
        "ix_option_opportunity_observation_cohort_measurement",
        "ix_option_event_signal_cohort_status",
        "ix_option_event_cohort_status_detected",
        "uq_option_event_active_ladder_slot",
        "ix_option_recovery_program_session_cohort_date",
        "ix_option_recovery_event_session_cohort_date",
        "ix_option_event_detector_run_cohort_time",
        "uq_option_recovery_current_cohort_objective",
    ):
        op.execute(f"DROP INDEX IF EXISTS analysis.{index}" if "recovery_paper" not in index else f"DROP INDEX IF EXISTS app.{index}")
    op.execute("DROP TABLE IF EXISTS analysis.option_recovery_program_session")
    op.execute("DROP TABLE IF EXISTS analysis.option_recovery_event_session_quality")
    op.execute("DROP TABLE IF EXISTS analysis.option_event_detector_run")
    op.execute("ALTER TABLE analysis.option_event DROP CONSTRAINT IF EXISTS ck_option_event_data_quality_status")
    op.execute("ALTER TABLE analysis.option_event DROP CONSTRAINT IF EXISTS ck_option_event_status")
    op.execute("ALTER TABLE analysis.option_event ADD CONSTRAINT ck_option_event_status CHECK (status IN ('active', 'deferred_capacity', 'closed'))")
    op.execute("ALTER TABLE app.paper_order DROP COLUMN IF EXISTS cohort_id")
    op.execute("ALTER TABLE analysis.option_event_agent_batch DROP COLUMN IF EXISTS cohort_id")
    op.execute("ALTER TABLE analysis.option_opportunity_observation DROP COLUMN IF EXISTS cohort_id")
    op.execute("ALTER TABLE analysis.option_event_signal DROP COLUMN IF EXISTS cohort_id")
    op.execute("ALTER TABLE analysis.option_event_capture DROP COLUMN IF EXISTS original_continuity_pct")
    op.execute("ALTER TABLE analysis.option_event_capture DROP COLUMN IF EXISTS canonical_continuity_pct")
    op.execute("ALTER TABLE analysis.option_event_contract DROP COLUMN IF EXISTS retired_reason")
    op.execute("ALTER TABLE analysis.option_event_contract DROP COLUMN IF EXISTS ladder_slot_key")
    for column in (
        "capacity_defer_reason", "priority_components", "invalidation_reason", "invalidated_at",
        "reference_available_at", "reference_source_id", "reference_trading_date",
        "quote_age_minutes", "trigger_reason", "data_quality_status", "cohort_id",
    ):
        op.execute(f"ALTER TABLE analysis.option_event DROP COLUMN IF EXISTS {column}")
    op.execute("DROP TABLE IF EXISTS analysis.option_recovery_cohort")
