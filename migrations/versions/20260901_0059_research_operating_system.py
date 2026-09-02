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

        CREATE TABLE analysis.experiment_manifest (
            experiment_family_id UUID PRIMARY KEY REFERENCES analysis.experiment_family(id),
            expected_trial_count INTEGER NOT NULL CHECK (expected_trial_count BETWEEN 1 AND 10000),
            expected_trial_keys JSONB NOT NULL,
            manifest_hash CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (jsonb_typeof(expected_trial_keys) = 'array'),
            CHECK (available_at <= created_at)
        );

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

        CREATE TABLE analysis.trial_universe_manifest (
            research_trial_id UUID PRIMARY KEY REFERENCES analysis.research_trial(id),
            cutoff TIMESTAMPTZ NOT NULL,
            expected_member_count INTEGER NOT NULL CHECK (expected_member_count BETWEEN 0 AND 10000),
            expected_members JSONB NOT NULL,
            manifest_hash CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (jsonb_typeof(expected_members) = 'array'),
            CHECK (available_at <= cutoff)
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
            CHECK (generated_at <= input_cutoff),
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
            ADD COLUMN artifact_hash CHAR(64),
            ADD COLUMN research_required BOOLEAN NOT NULL DEFAULT FALSE;
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
            IF TG_OP <> 'INSERT' AND (OLD.status <> 'running' OR NEW.experiment_family_id IS DISTINCT FROM OLD.experiment_family_id
               OR NEW.trial_key IS DISTINCT FROM OLD.trial_key
               OR NEW.input_cutoff IS DISTINCT FROM OLD.input_cutoff
               OR NEW.input_hash IS DISTINCT FROM OLD.input_hash
               OR NEW.parameters IS DISTINCT FROM OLD.parameters) THEN
                RAISE EXCEPTION 'research trial authority is immutable after creation';
            END IF;
            IF NEW.status <> 'running' AND (
                NOT EXISTS (SELECT 1 FROM analysis.trial_result result WHERE result.research_trial_id = OLD.id)
                OR NOT analysis.research_trial_universe_complete(OLD.id)
            ) THEN
                RAISE EXCEPTION 'terminal research trial requires a result and complete universe manifest';
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

        CREATE OR REPLACE FUNCTION analysis.enforce_research_universe_pit()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE manifest_cutoff TIMESTAMPTZ;
        BEGIN
            SELECT manifest.cutoff INTO manifest_cutoff
            FROM analysis.trial_universe_manifest manifest
            WHERE manifest.research_trial_id = NEW.research_trial_id;
            IF manifest_cutoff IS NULL OR NEW.cutoff <> manifest_cutoff
               OR NEW.observed_at > manifest_cutoff OR NEW.available_at > manifest_cutoff THEN
                RAISE EXCEPTION 'universe observation is outside its point-in-time cutoff';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_research_universe_pit
            BEFORE INSERT ON analysis.universe_observation
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_universe_pit();

        CREATE OR REPLACE FUNCTION analysis.research_trial_universe_complete(trial_id UUID)
        RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
            SELECT EXISTS (
                SELECT 1
                FROM analysis.trial_universe_manifest manifest
                WHERE manifest.research_trial_id = trial_id
                  AND manifest.expected_member_count = (
                      SELECT count(*) FROM analysis.universe_observation observation
                      WHERE observation.research_trial_id = trial_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM jsonb_array_elements_text(manifest.expected_members) expected(member)
                      WHERE NOT EXISTS (
                          SELECT 1 FROM analysis.universe_observation observation
                          WHERE observation.research_trial_id = trial_id
                            AND observation.instrument_id::text = expected.member
                      )
                  )
            );
        $$;

        CREATE OR REPLACE FUNCTION analysis.research_family_complete(family_id UUID)
        RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
            SELECT EXISTS (
                SELECT 1 FROM analysis.experiment_manifest manifest
                WHERE manifest.experiment_family_id = family_id
                  AND manifest.expected_trial_count = (
                      SELECT count(*) FROM analysis.research_trial trial
                      WHERE trial.experiment_family_id = family_id
                  )
                  AND manifest.expected_trial_keys = COALESCE((
                      SELECT jsonb_agg(trial.trial_key ORDER BY trial.trial_key)
                      FROM analysis.research_trial trial
                      WHERE trial.experiment_family_id = family_id
                  ), '[]'::jsonb)
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis.research_trial trial
                      WHERE trial.experiment_family_id = family_id
                        AND (trial.status = 'running' OR NOT EXISTS (
                            SELECT 1 FROM analysis.trial_result result WHERE result.research_trial_id = trial.id
                        ))
                  )
            );
        $$;

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
            IF NEW.research_trial_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM analysis.research_trial trial
                WHERE trial.id = NEW.research_trial_id
                  AND trial.status = 'succeeded'
                  AND analysis.research_trial_universe_complete(trial.id)
                  AND analysis.research_family_complete(trial.experiment_family_id)
                  AND NEW.artifact_id IS NOT NULL AND NEW.artifact_hash IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'validation dossier research trial or manifest is incomplete';
            END IF;
            NEW.sealed_at := COALESCE(NEW.sealed_at, clock_timestamp());
            IF NEW.research_trial_id IS NOT NULL AND NEW.sealed_at > (SELECT input_cutoff FROM analysis.research_trial WHERE id = NEW.research_trial_id) THEN
                RAISE EXCEPTION 'validation dossier seal is newer than its point-in-time cutoff';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_validation_dossier_seal
            BEFORE INSERT OR UPDATE OR DELETE ON analysis.validation_dossier
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_validation_dossier_seal();

        CREATE OR REPLACE FUNCTION analysis.enforce_research_revision_promotion()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status = 'active' AND (NEW.research_required OR NEW.hypothesis_id IS NOT NULL OR NEW.experiment_family_id IS NOT NULL) THEN
                IF NEW.hypothesis_id IS NULL OR NEW.experiment_family_id IS NULL THEN
                    RAISE EXCEPTION 'research strategy promotion requires hypothesis and experiment family lineage';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM analysis.validation_dossier dossier
                    JOIN analysis.research_trial trial ON trial.id = dossier.research_trial_id
                    JOIN analysis.experiment_family family
                      ON family.id = trial.experiment_family_id
                     AND family.hypothesis_id = NEW.hypothesis_id
                    WHERE dossier.strategy_revision_id = NEW.id AND dossier.status = 'sealed'
                      AND trial.status = 'succeeded'
                      AND trial.experiment_family_id = NEW.experiment_family_id
                      AND dossier.artifact_id = NEW.artifact_id
                      AND dossier.artifact_hash = NEW.artifact_hash
                      AND dossier.compiled_policy->>'paper_only' = 'true'
                      AND analysis.research_trial_universe_complete(trial.id)
                      AND analysis.research_family_complete(trial.experiment_family_id)
                      AND (SELECT count(*) FROM analysis.validation_gate_result gate WHERE gate.dossier_id = dossier.id AND gate.verdict = 'pass') = 5
                ) THEN
                    RAISE EXCEPTION 'research strategy promotion requires sealed dossier, complete manifests, five gates, matching artifact, and paper-only policy';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_research_revision_promotion
            BEFORE INSERT OR UPDATE OF status, research_required, hypothesis_id,
                experiment_family_id, artifact_id, artifact_hash ON analysis.strategy_revision
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_revision_promotion();

        CREATE OR REPLACE FUNCTION analysis.enforce_research_result_pit()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE cutoff TIMESTAMPTZ;
        BEGIN
            SELECT input_cutoff INTO cutoff FROM analysis.research_trial WHERE id = NEW.research_trial_id;
            IF cutoff IS NULL OR NEW.observed_at > cutoff OR NEW.available_at > cutoff THEN
                RAISE EXCEPTION 'research result is not point-in-time available';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_research_result_pit
            BEFORE INSERT ON analysis.trial_result
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_result_pit();

        CREATE OR REPLACE FUNCTION analysis.enforce_research_gate_pit()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE cutoff TIMESTAMPTZ;
        BEGIN
            SELECT trial.input_cutoff INTO cutoff
            FROM analysis.validation_dossier dossier
            JOIN analysis.research_trial trial ON trial.id = dossier.research_trial_id
            WHERE dossier.id = NEW.dossier_id;
            IF cutoff IS NOT NULL AND (NEW.available_at > cutoff OR NEW.evaluated_at > cutoff) THEN
                RAISE EXCEPTION 'validation gate is not point-in-time available';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_research_gate_pit
            BEFORE INSERT ON analysis.validation_gate_result
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_gate_pit();

        CREATE OR REPLACE FUNCTION analysis.enforce_research_manifest_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'research manifests are immutable';
        END;
        $$;
        CREATE TRIGGER enforce_experiment_manifest_immutable
            BEFORE UPDATE OR DELETE ON analysis.experiment_manifest
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_manifest_immutable();
        CREATE TRIGGER enforce_trial_universe_manifest_immutable
            BEFORE UPDATE OR DELETE ON analysis.trial_universe_manifest
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_manifest_immutable();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS enforce_research_revision_promotion ON analysis.strategy_revision;
        DROP FUNCTION IF EXISTS analysis.enforce_research_revision_promotion();
        DROP TRIGGER IF EXISTS enforce_trial_universe_manifest_immutable ON analysis.trial_universe_manifest;
        DROP TRIGGER IF EXISTS enforce_experiment_manifest_immutable ON analysis.experiment_manifest;
        DROP FUNCTION IF EXISTS analysis.enforce_research_manifest_immutable();
        DROP TRIGGER IF EXISTS enforce_research_result_pit ON analysis.trial_result;
        DROP FUNCTION IF EXISTS analysis.enforce_research_result_pit();
        DROP TRIGGER IF EXISTS enforce_research_gate_pit ON analysis.validation_gate_result;
        DROP FUNCTION IF EXISTS analysis.enforce_research_gate_pit();
        DROP TRIGGER IF EXISTS enforce_validation_dossier_seal ON analysis.validation_dossier;
        DROP FUNCTION IF EXISTS analysis.enforce_validation_dossier_seal();
        DROP TRIGGER IF EXISTS enforce_universe_observation_immutable ON analysis.universe_observation;
        DROP TRIGGER IF EXISTS enforce_research_universe_pit ON analysis.universe_observation;
        DROP FUNCTION IF EXISTS analysis.enforce_research_universe_pit();
        DROP TRIGGER IF EXISTS enforce_strategy_forecast_immutable ON analysis.strategy_forecast;
        DROP TRIGGER IF EXISTS enforce_validation_gate_result_immutable ON analysis.validation_gate_result;
        DROP TRIGGER IF EXISTS enforce_trial_result_immutable ON analysis.trial_result;
        DROP FUNCTION IF EXISTS analysis.enforce_research_rows_immutable();
        DROP TRIGGER IF EXISTS enforce_research_trial_terminal_immutability ON analysis.research_trial;
        DROP FUNCTION IF EXISTS analysis.enforce_research_trial_terminal_immutability();
        DROP FUNCTION IF EXISTS analysis.research_family_complete(UUID);
        DROP FUNCTION IF EXISTS analysis.research_trial_universe_complete(UUID);
        ALTER TABLE analysis.strategy_revision DROP COLUMN IF EXISTS research_required;
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
        DROP TABLE IF EXISTS analysis.trial_universe_manifest;
        DROP TABLE IF EXISTS analysis.strategy_forecast;
        DROP TABLE IF EXISTS analysis.validation_gate_result;
        DROP TABLE IF EXISTS analysis.validation_dossier;
        DROP TABLE IF EXISTS analysis.trial_result;
        DROP TABLE IF EXISTS analysis.research_trial;
        DROP TABLE IF EXISTS analysis.experiment_manifest;
        DROP TABLE IF EXISTS analysis.experiment_family;
        DROP TABLE IF EXISTS analysis.hypothesis;
        """
    )
