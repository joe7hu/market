"""Add the immutable, point-in-time research operating system."""

from __future__ import annotations

from alembic import op


revision = "20260901_0059"
down_revision = "20260830_0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE analysis.hypothesis (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            hypothesis_key TEXT NOT NULL UNIQUE,
            statement TEXT NOT NULL,
            mechanism_class TEXT NOT NULL,
            falsification TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            input_hash CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            CHECK (available_at >= created_at)
        );
        CREATE INDEX ix_hypothesis_available ON analysis.hypothesis (available_at, id);

        CREATE TABLE analysis.experiment_family (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            hypothesis_id UUID NOT NULL REFERENCES analysis.hypothesis(id),
            family_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            design JSONB NOT NULL DEFAULT '{}'::jsonb,
            controls JSONB NOT NULL DEFAULT '{}'::jsonb,
            status TEXT NOT NULL DEFAULT 'active',
            input_hash CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (hypothesis_id, name)
        );
        CREATE INDEX ix_experiment_family_hypothesis ON analysis.experiment_family (hypothesis_id, id);

        CREATE TABLE analysis.research_trial (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            experiment_family_id UUID NOT NULL REFERENCES analysis.experiment_family(id),
            trial_key TEXT NOT NULL,
            input_cutoff TIMESTAMPTZ NOT NULL,
            code_version TEXT NOT NULL,
            input_hash CHAR(64) NOT NULL,
            parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
            status TEXT NOT NULL DEFAULT 'running',
            failure_reason TEXT,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            outcome JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (experiment_family_id, trial_key),
            CHECK (status IN ('running', 'succeeded', 'failed', 'rejected')),
            CHECK (finished_at IS NULL OR finished_at >= started_at),
            CHECK (available_at <= input_cutoff)
        );
        CREATE INDEX ix_research_trial_family_cutoff ON analysis.research_trial (experiment_family_id, input_cutoff, id);

        CREATE TABLE analysis.trial_result (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            research_trial_id UUID NOT NULL REFERENCES analysis.research_trial(id),
            result_kind TEXT NOT NULL,
            result_version INTEGER NOT NULL DEFAULT 1 CHECK (result_version > 0),
            observed_at TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL,
            metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
            outcome JSONB NOT NULL DEFAULT '{}'::jsonb,
            input_hash CHAR(64) NOT NULL,
            UNIQUE (research_trial_id, result_kind, result_version),
            CHECK (available_at <= observed_at)
        );

        CREATE TABLE analysis.validation_dossier (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            strategy_revision_id BIGINT NOT NULL REFERENCES analysis.strategy_revision(id),
            research_trial_id UUID REFERENCES analysis.research_trial(id),
            status TEXT NOT NULL DEFAULT 'draft',
            sections JSONB NOT NULL DEFAULT '{}'::jsonb,
            compiled_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
            artifact_id TEXT,
            artifact_hash CHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            sealed_at TIMESTAMPTZ,
            UNIQUE (strategy_revision_id),
            CHECK (status IN ('draft', 'sealed', 'rejected'))
        );

        CREATE TABLE analysis.validation_gate_result (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            dossier_id UUID NOT NULL REFERENCES analysis.validation_dossier(id),
            gate_code TEXT NOT NULL,
            verdict TEXT NOT NULL,
            metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
            evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            evaluated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (dossier_id, gate_code),
            CHECK (gate_code IN (
                'pit_integrity', 'denominator_completeness',
                'oos_predictive_validity', 'falsification_and_robustness',
                'economic_promotability'
            )),
            CHECK (verdict IN ('pass', 'fail', 'unavailable')),
            CHECK (available_at <= evaluated_at)
        );

        CREATE TABLE analysis.strategy_forecast (
            id TEXT PRIMARY KEY,
            strategy_revision_id BIGINT NOT NULL REFERENCES analysis.strategy_revision(id),
            strategy_evaluation_id UUID REFERENCES analysis.strategy_evaluation(id),
            instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(id),
            opportunity_episode_id TEXT NOT NULL,
            target TEXT NOT NULL,
            horizon TEXT NOT NULL,
            forecast_value DOUBLE PRECISION,
            forecast_range JSONB,
            forecast_distribution JSONB,
            probability_semantics TEXT,
            model_artifact_id TEXT NOT NULL,
            artifact_hash CHAR(64) NOT NULL,
            input_hash CHAR(64) NOT NULL,
            as_of TIMESTAMPTZ NOT NULL,
            input_cutoff TIMESTAMPTZ NOT NULL,
            generated_at TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL DEFAULT 'available',
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            CHECK (as_of = input_cutoff),
            CHECK (available_at <= input_cutoff),
            CHECK (forecast_value IS NOT NULL OR forecast_range IS NOT NULL OR forecast_distribution IS NOT NULL)
        );
        CREATE UNIQUE INDEX uq_strategy_forecast_content
            ON analysis.strategy_forecast (strategy_revision_id, instrument_id, opportunity_episode_id, horizon, input_cutoff, artifact_hash);

        CREATE TABLE analysis.universe_observation (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            research_trial_id UUID NOT NULL REFERENCES analysis.research_trial(id),
            instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(id),
            cutoff TIMESTAMPTZ NOT NULL,
            eligible BOOLEAN NOT NULL,
            rank INTEGER,
            candidate_score DOUBLE PRECISION,
            exclusion_reason TEXT,
            observed_at TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL,
            input_hash CHAR(64) NOT NULL,
            outcome JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (research_trial_id, cutoff, instrument_id),
            CHECK (rank IS NULL OR rank > 0),
            CHECK (available_at <= cutoff),
            CHECK (eligible OR exclusion_reason IS NOT NULL)
        );
        CREATE INDEX ix_universe_observation_trial_cutoff
            ON analysis.universe_observation (research_trial_id, cutoff, rank NULLS LAST, instrument_id);
        CREATE INDEX ix_strategy_forecast_revision_cutoff
            ON analysis.strategy_forecast (strategy_revision_id, input_cutoff, instrument_id);

        ALTER TABLE analysis.strategy_revision
            ADD COLUMN hypothesis_id UUID REFERENCES analysis.hypothesis(id),
            ADD COLUMN experiment_family_id UUID REFERENCES analysis.experiment_family(id),
            ADD COLUMN artifact_id TEXT,
            ADD COLUMN artifact_hash CHAR(64);
        ALTER TABLE analysis.strategy_evaluation
            ADD COLUMN hypothesis_id UUID REFERENCES analysis.hypothesis(id),
            ADD COLUMN experiment_family_id UUID REFERENCES analysis.experiment_family(id),
            ADD COLUMN research_trial_id UUID REFERENCES analysis.research_trial(id),
            ADD COLUMN validation_dossier_id UUID REFERENCES analysis.validation_dossier(id),
            ADD COLUMN artifact_id TEXT,
            ADD COLUMN artifact_hash CHAR(64),
            ADD COLUMN input_hash CHAR(64),
            ADD COLUMN lineage JSONB NOT NULL DEFAULT '{}'::jsonb;

        CREATE OR REPLACE FUNCTION analysis.enforce_research_trial_terminal_immutability()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'terminal research trials are immutable';
            END IF;
            IF OLD.status <> 'running' OR NEW.experiment_family_id IS DISTINCT FROM OLD.experiment_family_id
               OR NEW.trial_key IS DISTINCT FROM OLD.trial_key
               OR NEW.input_cutoff IS DISTINCT FROM OLD.input_cutoff
               OR NEW.input_hash IS DISTINCT FROM OLD.input_hash
               OR NEW.parameters IS DISTINCT FROM OLD.parameters THEN
                RAISE EXCEPTION 'research trial authority is immutable after creation';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_research_trial_terminal_immutability
            BEFORE UPDATE OR DELETE ON analysis.research_trial
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_trial_terminal_immutability();

        CREATE OR REPLACE FUNCTION analysis.enforce_research_rows_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'research authority is immutable';
        END;
        $$;
        CREATE TRIGGER enforce_trial_result_immutable
            BEFORE UPDATE OR DELETE ON analysis.trial_result
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_rows_immutable();
        CREATE TRIGGER enforce_validation_gate_result_immutable
            BEFORE UPDATE OR DELETE ON analysis.validation_gate_result
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_rows_immutable();
        CREATE TRIGGER enforce_strategy_forecast_immutable
            BEFORE UPDATE OR DELETE ON analysis.strategy_forecast
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_rows_immutable();
        CREATE TRIGGER enforce_universe_observation_immutable
            BEFORE UPDATE OR DELETE ON analysis.universe_observation
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_rows_immutable();

        CREATE OR REPLACE FUNCTION analysis.enforce_validation_dossier_seal()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE gate_count INTEGER; passing_count INTEGER;
        BEGIN
            IF TG_OP = 'DELETE' OR OLD.status IN ('sealed', 'rejected') THEN
                RAISE EXCEPTION 'validation dossiers are immutable';
            END IF;
            IF NEW.status <> 'sealed' THEN
                RETURN NEW;
            END IF;
            IF jsonb_typeof(NEW.sections) <> 'object'
               OR NOT (NEW.sections ?& ARRAY['hypothesis', 'mechanism', 'falsification', 'controls', 'validation', 'economics', 'lineage']) THEN
                RAISE EXCEPTION 'validation dossier mandatory sections are incomplete';
            END IF;
            SELECT count(*), count(*) FILTER (WHERE verdict = 'pass')
              INTO gate_count, passing_count
              FROM analysis.validation_gate_result WHERE dossier_id = NEW.id;
            IF gate_count <> 5 OR passing_count <> 5 THEN
                RAISE EXCEPTION 'validation dossier requires all five passing gates';
            END IF;
            NEW.sealed_at := COALESCE(NEW.sealed_at, clock_timestamp());
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_validation_dossier_seal
            BEFORE UPDATE OR DELETE ON analysis.validation_dossier
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_validation_dossier_seal();

        CREATE OR REPLACE FUNCTION analysis.enforce_research_revision_promotion()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status = 'active' AND NEW.hypothesis_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM analysis.validation_dossier
                   WHERE strategy_revision_id = NEW.id AND status = 'sealed'
               ) THEN
                RAISE EXCEPTION 'research strategy promotion requires a sealed validation dossier';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_research_revision_promotion
            BEFORE INSERT OR UPDATE OF status ON analysis.strategy_revision
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_revision_promotion();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS enforce_research_revision_promotion ON analysis.strategy_revision;
        DROP FUNCTION IF EXISTS analysis.enforce_research_revision_promotion();
        DROP TRIGGER IF EXISTS enforce_validation_dossier_seal ON analysis.validation_dossier;
        DROP FUNCTION IF EXISTS analysis.enforce_validation_dossier_seal();
        DROP TRIGGER IF EXISTS enforce_universe_observation_immutable ON analysis.universe_observation;
        DROP TRIGGER IF EXISTS enforce_strategy_forecast_immutable ON analysis.strategy_forecast;
        DROP TRIGGER IF EXISTS enforce_validation_gate_result_immutable ON analysis.validation_gate_result;
        DROP TRIGGER IF EXISTS enforce_trial_result_immutable ON analysis.trial_result;
        DROP FUNCTION IF EXISTS analysis.enforce_research_rows_immutable();
        DROP TRIGGER IF EXISTS enforce_research_trial_terminal_immutability ON analysis.research_trial;
        DROP FUNCTION IF EXISTS analysis.enforce_research_trial_terminal_immutability();
        ALTER TABLE analysis.strategy_evaluation
            DROP COLUMN IF EXISTS lineage, DROP COLUMN IF EXISTS input_hash,
            DROP COLUMN IF EXISTS artifact_hash, DROP COLUMN IF EXISTS artifact_id,
            DROP COLUMN IF EXISTS validation_dossier_id, DROP COLUMN IF EXISTS research_trial_id,
            DROP COLUMN IF EXISTS experiment_family_id, DROP COLUMN IF EXISTS hypothesis_id;
        ALTER TABLE analysis.strategy_revision
            DROP COLUMN IF EXISTS artifact_hash, DROP COLUMN IF EXISTS artifact_id,
            DROP COLUMN IF EXISTS experiment_family_id, DROP COLUMN IF EXISTS hypothesis_id;
        DROP INDEX IF EXISTS analysis.ix_universe_observation_trial_cutoff;
        DROP INDEX IF EXISTS analysis.ix_strategy_forecast_revision_cutoff;
        DROP INDEX IF EXISTS analysis.ix_research_trial_family_cutoff;
        DROP INDEX IF EXISTS analysis.ix_experiment_family_hypothesis;
        DROP INDEX IF EXISTS analysis.ix_hypothesis_available;
        DROP INDEX IF EXISTS analysis.uq_strategy_forecast_content;
        DROP TABLE IF EXISTS analysis.universe_observation;
        DROP TABLE IF EXISTS analysis.strategy_forecast;
        DROP TABLE IF EXISTS analysis.validation_gate_result;
        DROP TABLE IF EXISTS analysis.validation_dossier;
        DROP TABLE IF EXISTS analysis.trial_result;
        DROP TABLE IF EXISTS analysis.research_trial;
        DROP TABLE IF EXISTS analysis.experiment_family;
        DROP TABLE IF EXISTS analysis.hypothesis;
        """
    )
