"""Add the Phase 3 strategy factory extensions to the existing research authority."""

from __future__ import annotations

from alembic import op


revision = "20260902_0070"
down_revision = "20260902_0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE analysis.strategy_revision
            ADD COLUMN mechanism_class TEXT,
            ADD COLUMN economic_mechanism TEXT,
            ADD COLUMN falsification_rule TEXT,
            ADD COLUMN source_definition_version TEXT,
            ADD COLUMN promotability TEXT NOT NULL DEFAULT 'standard',
            ADD COLUMN actionability TEXT NOT NULL DEFAULT 'daily_research',
            ADD COLUMN p3_enabled BOOLEAN NOT NULL DEFAULT false;

        ALTER TABLE analysis.strategy_revision
            ADD CONSTRAINT strategy_revision_promotability_check
                CHECK (promotability IN ('standard', 'negative_control', 'registration_only', 'exposure_sleeve')),
            ADD CONSTRAINT strategy_revision_actionability_check
                CHECK (actionability IN ('daily_research', 'shadow_only', 'research_only', 'registration_only'));

        CREATE TABLE analysis.strategy_manifest (
            strategy_revision_id BIGINT PRIMARY KEY REFERENCES analysis.strategy_revision(id),
            source_definition_version TEXT NOT NULL,
            source_manifest JSONB NOT NULL,
            data_manifest JSONB NOT NULL,
            cost_manifest JSONB NOT NULL,
            capacity_manifest JSONB NOT NULL,
            failure_manifest JSONB NOT NULL,
            manifest_hash CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CHECK (jsonb_typeof(source_manifest) = 'object' AND source_manifest <> '{}'::jsonb),
            CHECK (jsonb_typeof(data_manifest) = 'object' AND data_manifest <> '{}'::jsonb),
            CHECK (jsonb_typeof(cost_manifest) = 'object' AND cost_manifest <> '{}'::jsonb),
            CHECK (jsonb_typeof(capacity_manifest) = 'object' AND capacity_manifest <> '{}'::jsonb),
            CHECK (jsonb_typeof(failure_manifest) = 'object' AND failure_manifest <> '{}'::jsonb),
            CHECK (manifest_hash ~ '^[0-9a-f]{64}$')
        );

        CREATE TABLE analysis.strategy_pnl_tape (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            strategy_revision_id BIGINT NOT NULL REFERENCES analysis.strategy_revision(id),
            instrument_id BIGINT REFERENCES catalog.instrument(id),
            pnl_date DATE NOT NULL,
            input_cutoff TIMESTAMPTZ NOT NULL,
            gross_return DOUBLE PRECISION,
            cost DOUBLE PRECISION,
            net_return DOUBLE PRECISION,
            tail_return DOUBLE PRECISION,
            regime TEXT,
            observed_at TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL,
            input_hash CHAR(64) NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (strategy_revision_id, instrument_id, pnl_date, input_hash),
            CHECK (available_at <= observed_at),
            CHECK (input_hash ~ '^[0-9a-f]{64}$')
        );
        CREATE INDEX ix_strategy_pnl_tape_date
            ON analysis.strategy_pnl_tape (strategy_revision_id, pnl_date, instrument_id);

        CREATE TABLE analysis.strategy_monitoring_evidence (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            strategy_revision_id BIGINT NOT NULL REFERENCES analysis.strategy_revision(id),
            evidence_kind TEXT NOT NULL,
            input_cutoff TIMESTAMPTZ NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL,
            input_hash CHAR(64) NOT NULL,
            metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
            evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (strategy_revision_id, evidence_kind, input_cutoff, input_hash),
            CHECK (evidence_kind IN ('correlation', 'tail_correlation', 'crowding', 'capacity', 'decay', 'regime')),
            CHECK (available_at <= observed_at),
            CHECK (input_hash ~ '^[0-9a-f]{64}$')
        );
        CREATE INDEX ix_strategy_monitoring_revision_kind
            ON analysis.strategy_monitoring_evidence (strategy_revision_id, evidence_kind, input_cutoff DESC);

        CREATE TABLE analysis.strategy_comparison (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            champion_revision_id BIGINT NOT NULL REFERENCES analysis.strategy_revision(id),
            challenger_revision_id BIGINT NOT NULL REFERENCES analysis.strategy_revision(id),
            input_cutoff TIMESTAMPTZ NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL,
            input_hash CHAR(64) NOT NULL,
            distinctness TEXT NOT NULL,
            explanation TEXT NOT NULL,
            metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (champion_revision_id, challenger_revision_id, input_cutoff, input_hash),
            CHECK (champion_revision_id <> challenger_revision_id),
            CHECK (distinctness IN ('distinct', 'replica', 'exposure_sleeve', 'inconclusive')),
            CHECK (available_at <= observed_at),
            CHECK (input_hash ~ '^[0-9a-f]{64}$')
        );

        CREATE OR REPLACE FUNCTION analysis.research_trial_p3_denominator_complete(trial_uuid UUID)
        RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
            SELECT analysis.research_trial_universe_complete(trial_uuid)
               AND NOT EXISTS (
                   SELECT 1 FROM analysis.universe_observation observation
                   WHERE observation.research_trial_id = trial_uuid
                     AND (jsonb_typeof(observation.outcome) <> 'object'
                          OR observation.outcome = '{}'::jsonb)
               )
        $$;

        CREATE OR REPLACE FUNCTION analysis.enforce_phase3_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Phase 3 research evidence is immutable';
        END;
        $$;
        CREATE TRIGGER enforce_strategy_manifest_immutable
            BEFORE UPDATE OR DELETE ON analysis.strategy_manifest
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_immutable();
        CREATE TRIGGER enforce_strategy_pnl_tape_immutable
            BEFORE UPDATE OR DELETE ON analysis.strategy_pnl_tape
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_immutable();
        CREATE TRIGGER enforce_strategy_monitoring_immutable
            BEFORE UPDATE OR DELETE ON analysis.strategy_monitoring_evidence
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_immutable();
        CREATE TRIGGER enforce_strategy_comparison_immutable
            BEFORE UPDATE OR DELETE ON analysis.strategy_comparison
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_immutable();

        CREATE OR REPLACE FUNCTION analysis.enforce_phase3_strategy_status()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE evidence_count INTEGER;
        BEGIN
            IF NEW.p3_enabled AND NEW.status IN ('active', 'promoted') THEN
                IF NEW.strategy_key = 'martingale_v1' OR NEW.promotability = 'negative_control' THEN
                    RAISE EXCEPTION 'martingale_v1 is a permanent non-promotable negative control';
                END IF;
                IF NEW.promotability = 'registration_only' THEN
                    RAISE EXCEPTION 'registration-only strategy cannot be promoted';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM analysis.strategy_manifest manifest WHERE manifest.strategy_revision_id = NEW.id) THEN
                    RAISE EXCEPTION 'Phase 3 strategy promotion requires an immutable strategy manifest';
                END IF;
                IF NEW.promotability = 'standard' AND NOT EXISTS (
                    SELECT 1 FROM analysis.validation_dossier dossier
                    JOIN analysis.research_trial trial ON trial.id = dossier.research_trial_id
                    WHERE dossier.strategy_revision_id = NEW.id
                      AND dossier.status = 'sealed'
                      AND trial.status = 'succeeded'
                      AND analysis.research_trial_p3_denominator_complete(trial.id)
                ) THEN
                    RAISE EXCEPTION 'Phase 3 strategy promotion requires complete PIT denominator outcomes';
                END IF;
                SELECT count(DISTINCT evidence_kind) INTO evidence_count
                FROM analysis.strategy_monitoring_evidence
                WHERE strategy_revision_id = NEW.id;
                IF NEW.promotability = 'standard' AND evidence_count < 6 THEN
                    RAISE EXCEPTION 'Phase 3 active strategy requires correlation, tail, crowding, capacity, decay, and regime evidence';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_phase3_strategy_status
            BEFORE INSERT OR UPDATE OF status, promotability, p3_enabled ON analysis.strategy_revision
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_strategy_status();

        CREATE VIEW analysis.strategy_registry AS
        SELECT revision.id AS strategy_revision_id, revision.strategy_key, revision.revision,
               revision.name, revision.status, revision.mechanism_class,
               revision.economic_mechanism, revision.falsification_rule,
               revision.source_definition_version, revision.promotability,
               revision.actionability, revision.p3_enabled, revision.parameters,
               revision.created_at, revision.promoted_at, revision.supersedes_id,
               manifest.manifest_hash, manifest.available_at AS manifest_available_at
        FROM analysis.strategy_revision revision
        LEFT JOIN analysis.strategy_manifest manifest
          ON manifest.strategy_revision_id = revision.id;

        CREATE VIEW analysis.strategy_trial_accounting AS
        SELECT family.family_key, trial.id AS research_trial_id, trial.trial_key,
               trial.status, trial.input_cutoff, trial.available_at,
               manifest.expected_member_count,
               count(observation.id) AS observed_member_count,
               count(observation.id) FILTER (WHERE observation.outcome <> '{}'::jsonb) AS outcome_member_count,
               EXISTS (SELECT 1 FROM analysis.trial_result result WHERE result.research_trial_id = trial.id) AS has_result,
               analysis.research_trial_p3_denominator_complete(trial.id) AS denominator_complete,
               analysis.research_family_complete(trial.experiment_family_id) AS family_complete
        FROM analysis.research_trial trial
        JOIN analysis.experiment_family family ON family.id = trial.experiment_family_id
        LEFT JOIN analysis.trial_universe_manifest manifest ON manifest.research_trial_id = trial.id
        LEFT JOIN analysis.universe_observation observation ON observation.research_trial_id = trial.id
        GROUP BY family.family_key, trial.id, trial.trial_key, trial.status,
                 trial.input_cutoff, trial.available_at, manifest.expected_member_count,
                 trial.experiment_family_id;

        GRANT SELECT ON analysis.strategy_registry, analysis.strategy_trial_accounting,
                       analysis.strategy_manifest, analysis.strategy_pnl_tape,
                       analysis.strategy_monitoring_evidence, analysis.strategy_comparison
          TO market_app;
        GRANT INSERT ON analysis.strategy_manifest, analysis.strategy_pnl_tape,
                        analysis.strategy_monitoring_evidence, analysis.strategy_comparison
          TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP VIEW IF EXISTS analysis.strategy_trial_accounting;
        DROP VIEW IF EXISTS analysis.strategy_registry;
        DROP TRIGGER IF EXISTS enforce_phase3_strategy_status ON analysis.strategy_revision;
        DROP FUNCTION IF EXISTS analysis.enforce_phase3_strategy_status();
        DROP TRIGGER IF EXISTS enforce_strategy_comparison_immutable ON analysis.strategy_comparison;
        DROP TRIGGER IF EXISTS enforce_strategy_monitoring_immutable ON analysis.strategy_monitoring_evidence;
        DROP TRIGGER IF EXISTS enforce_strategy_pnl_tape_immutable ON analysis.strategy_pnl_tape;
        DROP TRIGGER IF EXISTS enforce_strategy_manifest_immutable ON analysis.strategy_manifest;
        DROP FUNCTION IF EXISTS analysis.enforce_phase3_immutable();
        DROP FUNCTION IF EXISTS analysis.research_trial_p3_denominator_complete(UUID);
        REVOKE SELECT ON analysis.strategy_registry, analysis.strategy_trial_accounting,
                          analysis.strategy_manifest, analysis.strategy_pnl_tape,
                          analysis.strategy_monitoring_evidence, analysis.strategy_comparison
          FROM market_app;
        REVOKE INSERT ON analysis.strategy_manifest, analysis.strategy_pnl_tape,
                           analysis.strategy_monitoring_evidence, analysis.strategy_comparison
          FROM market_app;
        DROP TABLE IF EXISTS analysis.strategy_comparison;
        DROP TABLE IF EXISTS analysis.strategy_monitoring_evidence;
        DROP TABLE IF EXISTS analysis.strategy_pnl_tape;
        DROP TABLE IF EXISTS analysis.strategy_manifest;
        ALTER TABLE analysis.strategy_revision
            DROP CONSTRAINT IF EXISTS strategy_revision_actionability_check,
            DROP CONSTRAINT IF EXISTS strategy_revision_promotability_check,
            DROP COLUMN IF EXISTS p3_enabled,
            DROP COLUMN IF EXISTS actionability,
            DROP COLUMN IF EXISTS promotability,
            DROP COLUMN IF EXISTS source_definition_version,
            DROP COLUMN IF EXISTS falsification_rule,
            DROP COLUMN IF EXISTS economic_mechanism,
            DROP COLUMN IF EXISTS mechanism_class;
        """
    )
