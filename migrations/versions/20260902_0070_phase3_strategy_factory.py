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
            ADD COLUMN strategy_family TEXT NOT NULL DEFAULT 'legacy',
            ADD COLUMN promotability TEXT NOT NULL DEFAULT 'standard',
            ADD COLUMN actionability TEXT NOT NULL DEFAULT 'daily_research',
            ADD COLUMN p3_enabled BOOLEAN NOT NULL DEFAULT false;

        ALTER TABLE analysis.strategy_revision
            ADD CONSTRAINT strategy_revision_promotability_check
                CHECK (promotability IN ('standard', 'negative_control', 'registration_only', 'exposure_sleeve')),
            ADD CONSTRAINT strategy_revision_actionability_check
                CHECK (actionability IN ('daily_research', 'shadow_only', 'research_only', 'registration_only')),
            ADD CONSTRAINT strategy_revision_family_check
                CHECK (strategy_family <> '');

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
        CREATE UNIQUE INDEX uq_strategy_manifest_revision_hash
            ON analysis.strategy_manifest (strategy_revision_id, manifest_hash);

        ALTER TABLE analysis.strategy_forecast
            ADD COLUMN research_trial_id UUID REFERENCES analysis.research_trial(id),
            ADD COLUMN trial_result_id UUID REFERENCES analysis.trial_result(id),
            ADD COLUMN universe_manifest_hash CHAR(64),
            ADD COLUMN result_hash CHAR(64),
            ADD CONSTRAINT strategy_forecast_p3_link_check CHECK (
                (research_trial_id IS NULL AND trial_result_id IS NULL
                 AND universe_manifest_hash IS NULL AND result_hash IS NULL)
                OR (research_trial_id IS NOT NULL AND trial_result_id IS NOT NULL
                    AND universe_manifest_hash IS NOT NULL AND result_hash IS NOT NULL)
            ),
            ADD CONSTRAINT strategy_forecast_p3_pit_check CHECK (
                research_trial_id IS NULL OR available_at <= input_cutoff
            );

        CREATE TABLE analysis.strategy_pnl_tape (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            strategy_revision_id BIGINT NOT NULL REFERENCES analysis.strategy_revision(id),
            instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(id),
            strategy_forecast_id TEXT NOT NULL REFERENCES analysis.strategy_forecast(id),
            research_trial_id UUID NOT NULL REFERENCES analysis.research_trial(id),
            trial_result_id UUID NOT NULL REFERENCES analysis.trial_result(id),
            universe_manifest_hash CHAR(64) NOT NULL,
            result_hash CHAR(64) NOT NULL,
            pnl_date DATE NOT NULL,
            input_cutoff TIMESTAMPTZ NOT NULL,
            gross_return DOUBLE PRECISION NOT NULL,
            cost DOUBLE PRECISION NOT NULL,
            net_return DOUBLE PRECISION NOT NULL,
            tail_return DOUBLE PRECISION,
            regime TEXT,
            observed_at TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL,
            input_hash CHAR(64) NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (strategy_revision_id, instrument_id, pnl_date, input_hash),
            UNIQUE (strategy_forecast_id),
            CHECK (available_at <= observed_at AND available_at <= input_cutoff),
            CHECK (input_hash ~ '^[0-9a-f]{64}$')
        );
        CREATE INDEX ix_strategy_pnl_tape_date
            ON analysis.strategy_pnl_tape (strategy_revision_id, pnl_date, instrument_id);

        CREATE TABLE analysis.strategy_monitoring_evidence (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            strategy_revision_id BIGINT NOT NULL REFERENCES analysis.strategy_revision(id),
            research_trial_id UUID NOT NULL REFERENCES analysis.research_trial(id),
            trial_result_id UUID NOT NULL REFERENCES analysis.trial_result(id),
            universe_manifest_hash CHAR(64) NOT NULL,
            result_hash CHAR(64) NOT NULL,
            evidence_kind TEXT NOT NULL,
            input_cutoff TIMESTAMPTZ NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL,
            input_hash CHAR(64) NOT NULL,
            metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
            evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            lineage JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (strategy_revision_id, evidence_kind, input_cutoff, input_hash),
            CHECK (evidence_kind IN ('correlation', 'tail_correlation', 'crowding', 'capacity', 'decay', 'regime')),
            CHECK (available_at <= observed_at AND available_at <= input_cutoff),
            CHECK (input_hash ~ '^[0-9a-f]{64}$')
        );
        CREATE INDEX ix_strategy_monitoring_revision_kind
            ON analysis.strategy_monitoring_evidence (strategy_revision_id, evidence_kind, input_cutoff DESC);

        CREATE TABLE analysis.strategy_comparison (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            champion_revision_id BIGINT NOT NULL REFERENCES analysis.strategy_revision(id),
            challenger_revision_id BIGINT NOT NULL REFERENCES analysis.strategy_revision(id),
            champion_trial_id UUID NOT NULL REFERENCES analysis.research_trial(id),
            challenger_trial_id UUID NOT NULL REFERENCES analysis.research_trial(id),
            champion_result_id UUID NOT NULL REFERENCES analysis.trial_result(id),
            challenger_result_id UUID NOT NULL REFERENCES analysis.trial_result(id),
            champion_result_hash CHAR(64) NOT NULL,
            challenger_result_hash CHAR(64) NOT NULL,
            champion_manifest_hash CHAR(64) NOT NULL,
            challenger_manifest_hash CHAR(64) NOT NULL,
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
            CHECK (available_at <= observed_at AND available_at <= input_cutoff),
            CHECK (input_hash ~ '^[0-9a-f]{64}$')
        );

        CREATE OR REPLACE FUNCTION analysis.research_trial_p3_denominator_complete(trial_uuid UUID)
        RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
            SELECT analysis.research_trial_universe_complete(trial_uuid)
               AND NOT EXISTS (
                   SELECT 1
                   FROM analysis.research_trial trial
                   JOIN analysis.trial_universe_manifest manifest
                     ON manifest.research_trial_id = trial.id
                   WHERE trial.id = trial_uuid
                     AND (trial.available_at > trial.input_cutoff
                          OR manifest.available_at > trial.input_cutoff)
               )
               AND NOT EXISTS (
                   SELECT 1 FROM analysis.universe_observation observation
                   JOIN analysis.research_trial trial ON trial.id = observation.research_trial_id
                   WHERE observation.research_trial_id = trial_uuid
                     AND (observation.observed_at > trial.input_cutoff
                          OR observation.available_at > trial.input_cutoff
                          OR jsonb_typeof(observation.outcome) <> 'object'
                          OR observation.outcome = '{}'::jsonb)
               )
        $$;

        CREATE OR REPLACE FUNCTION analysis.phase3_json_hash(payload JSONB)
        RETURNS TEXT LANGUAGE sql IMMUTABLE STRICT AS $$
            SELECT encode(public.digest(convert_to(payload::text, 'UTF8'), 'sha256'), 'hex')
        $$;

        CREATE OR REPLACE FUNCTION analysis.enforce_phase3_manifest_hash()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.manifest_hash := analysis.phase3_json_hash(jsonb_build_object(
                'source', NEW.source_manifest, 'data', NEW.data_manifest,
                'cost', NEW.cost_manifest, 'capacity', NEW.capacity_manifest,
                'failure', NEW.failure_manifest
            ));
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_strategy_manifest_hash
            BEFORE INSERT ON analysis.strategy_manifest
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_manifest_hash();

        CREATE OR REPLACE FUNCTION analysis.enforce_phase3_forecast_link()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE trial_input_hash TEXT; result_input_hash TEXT; manifest_digest TEXT;
        BEGIN
            IF NEW.research_trial_id IS NULL THEN
                IF EXISTS (SELECT 1 FROM analysis.strategy_revision revision
                           WHERE revision.id = NEW.strategy_revision_id AND revision.p3_enabled) THEN
                    RAISE EXCEPTION 'Phase 3 forecast requires trial, result, and manifest lineage';
                END IF;
                RETURN NEW;
            END IF;
            SELECT input_hash::TEXT INTO trial_input_hash
            FROM analysis.research_trial WHERE id = NEW.research_trial_id;
            SELECT input_hash::TEXT INTO result_input_hash
            FROM analysis.trial_result
            WHERE id = NEW.trial_result_id AND research_trial_id = NEW.research_trial_id;
            SELECT manifest_hash::TEXT INTO manifest_digest
            FROM analysis.strategy_manifest
            WHERE strategy_revision_id = NEW.strategy_revision_id;
            IF trial_input_hash IS NULL OR result_input_hash IS NULL
               OR manifest_digest IS NULL
               OR NEW.universe_manifest_hash::TEXT <> manifest_digest
               OR NEW.result_hash::TEXT <> result_input_hash
               OR NEW.input_cutoff > (SELECT input_cutoff FROM analysis.research_trial WHERE id = NEW.research_trial_id)
               OR NEW.available_at > NEW.input_cutoff THEN
                RAISE EXCEPTION 'Phase 3 forecast lineage or PIT clock is invalid';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_strategy_forecast_phase3_link
            BEFORE INSERT OR UPDATE ON analysis.strategy_forecast
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_forecast_link();

        CREATE OR REPLACE FUNCTION analysis.enforce_phase3_pnl_tape()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE forecast analysis.strategy_forecast%ROWTYPE;
              result_input_hash TEXT; manifest_digest TEXT; trial_input_cutoff TIMESTAMPTZ;
        BEGIN
            SELECT * INTO forecast FROM analysis.strategy_forecast
            WHERE id = NEW.strategy_forecast_id;
            SELECT input_cutoff INTO trial_input_cutoff FROM analysis.research_trial
            WHERE id = NEW.research_trial_id;
            SELECT input_hash::TEXT INTO result_input_hash FROM analysis.trial_result
            WHERE id = NEW.trial_result_id AND research_trial_id = NEW.research_trial_id;
            SELECT manifest_hash::TEXT INTO manifest_digest FROM analysis.strategy_manifest
            WHERE strategy_revision_id = NEW.strategy_revision_id;
            IF forecast.id IS NULL OR forecast.strategy_revision_id <> NEW.strategy_revision_id
               OR forecast.instrument_id <> NEW.instrument_id
               OR forecast.research_trial_id <> NEW.research_trial_id
               OR forecast.trial_result_id <> NEW.trial_result_id
               OR forecast.input_cutoff <> NEW.input_cutoff
               OR forecast.universe_manifest_hash::TEXT <> NEW.universe_manifest_hash::TEXT
               OR forecast.result_hash::TEXT <> NEW.result_hash::TEXT
               OR result_input_hash IS NULL OR manifest_digest IS NULL
               OR NEW.result_hash::TEXT <> result_input_hash
               OR NEW.universe_manifest_hash::TEXT <> manifest_digest
               OR trial_input_cutoff IS NULL OR NEW.input_cutoff > trial_input_cutoff
               OR NEW.available_at > NEW.input_cutoff THEN
                RAISE EXCEPTION 'Phase 3 P&L tape has invalid canonical lineage or PIT clock';
            END IF;
            NEW.input_hash := analysis.phase3_json_hash(jsonb_build_object(
                'strategy_revision_id', NEW.strategy_revision_id,
                'strategy_forecast_id', NEW.strategy_forecast_id,
                'research_trial_id', NEW.research_trial_id,
                'trial_result_id', NEW.trial_result_id,
                'universe_manifest_hash', NEW.universe_manifest_hash,
                'result_hash', NEW.result_hash, 'instrument_id', NEW.instrument_id,
                'pnl_date', NEW.pnl_date, 'input_cutoff', NEW.input_cutoff,
                'gross_return', NEW.gross_return, 'cost', NEW.cost,
                'net_return', NEW.net_return, 'tail_return', NEW.tail_return,
                'regime', NEW.regime, 'metadata', NEW.metadata
            ));
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_strategy_pnl_tape_lineage
            BEFORE INSERT ON analysis.strategy_pnl_tape
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_pnl_tape();

        CREATE OR REPLACE FUNCTION analysis.enforce_phase3_monitoring()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE result_input_hash TEXT; manifest_digest TEXT; trial_input_cutoff TIMESTAMPTZ;
              tape_count INTEGER;
              mean_return DOUBLE PRECISION; mean_cost DOUBLE PRECISION; tape_hashes JSONB;
        BEGIN
            SELECT input_hash::TEXT INTO result_input_hash FROM analysis.trial_result
            WHERE id = NEW.trial_result_id AND research_trial_id = NEW.research_trial_id;
            SELECT manifest_hash::TEXT INTO manifest_digest FROM analysis.strategy_manifest
            WHERE strategy_revision_id = NEW.strategy_revision_id;
            SELECT input_cutoff INTO trial_input_cutoff FROM analysis.research_trial
            WHERE id = NEW.research_trial_id;
            IF result_input_hash IS NULL OR manifest_digest IS NULL
               OR NEW.result_hash::TEXT <> result_input_hash
               OR NEW.universe_manifest_hash::TEXT <> manifest_digest
               OR trial_input_cutoff IS NULL OR NEW.input_cutoff > trial_input_cutoff
               OR NEW.available_at > NEW.input_cutoff THEN
                RAISE EXCEPTION 'Phase 3 monitoring evidence has invalid trial, manifest, or PIT lineage';
            END IF;
            SELECT count(*), avg(net_return), avg(cost),
                   COALESCE(jsonb_agg(to_jsonb(input_hash::TEXT) ORDER BY id), '[]'::jsonb)
              INTO tape_count, mean_return, mean_cost, tape_hashes
              FROM analysis.strategy_pnl_tape
             WHERE strategy_revision_id = NEW.strategy_revision_id
               AND research_trial_id = NEW.research_trial_id
               AND trial_result_id = NEW.trial_result_id
               AND universe_manifest_hash::TEXT = NEW.universe_manifest_hash::TEXT
               AND result_hash::TEXT = NEW.result_hash::TEXT
               AND available_at <= NEW.input_cutoff;
            IF tape_count = 0 THEN
                RAISE EXCEPTION 'Phase 3 monitoring evidence requires canonical P&L tape lineage';
            END IF;
            NEW.lineage := jsonb_build_object(
                'research_trial_id', NEW.research_trial_id,
                'trial_result_id', NEW.trial_result_id,
                'universe_manifest_hash', NEW.universe_manifest_hash,
                'pnl_input_hashes', tape_hashes,
                'generated_by', 'postgresql'
            );
            NEW.metrics := jsonb_build_object(
                'evidence_kind', NEW.evidence_kind,
                'pnl_observation_count', tape_count,
                'mean_net_return', mean_return,
                'mean_cost', mean_cost
            );
            NEW.evidence := jsonb_build_object(
                'canonical_pnl_input_hashes', tape_hashes,
                'generated_by', 'postgresql'
            );
            NEW.input_hash := analysis.phase3_json_hash(jsonb_build_object(
                'strategy_revision_id', NEW.strategy_revision_id,
                'research_trial_id', NEW.research_trial_id,
                'trial_result_id', NEW.trial_result_id,
                'universe_manifest_hash', NEW.universe_manifest_hash,
                'evidence_kind', NEW.evidence_kind, 'input_cutoff', NEW.input_cutoff,
                'metrics', NEW.metrics, 'evidence', NEW.evidence, 'lineage', NEW.lineage
            ));
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_strategy_monitoring_lineage
            BEFORE INSERT ON analysis.strategy_monitoring_evidence
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_monitoring();

        CREATE OR REPLACE FUNCTION analysis.enforce_phase3_comparison()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE champion_result TEXT; challenger_result TEXT; champion_manifest TEXT; challenger_manifest TEXT;
              champion_trial_cutoff TIMESTAMPTZ; challenger_trial_cutoff TIMESTAMPTZ;
              pair_count INTEGER; return_correlation DOUBLE PRECISION; computed_distinctness TEXT;
              champion_hashes JSONB; challenger_hashes JSONB; challenger_class TEXT;
        BEGIN
            SELECT input_hash::TEXT INTO champion_result FROM analysis.trial_result
            WHERE id = NEW.champion_result_id AND research_trial_id = NEW.champion_trial_id
              AND input_hash::TEXT = NEW.champion_result_hash::TEXT;
            SELECT input_hash::TEXT INTO challenger_result FROM analysis.trial_result
            WHERE id = NEW.challenger_result_id AND research_trial_id = NEW.challenger_trial_id
              AND input_hash::TEXT = NEW.challenger_result_hash::TEXT;
            SELECT manifest_hash::TEXT INTO champion_manifest FROM analysis.strategy_manifest
            WHERE strategy_revision_id = NEW.champion_revision_id
              AND manifest_hash::TEXT = NEW.champion_manifest_hash::TEXT;
            SELECT manifest_hash::TEXT INTO challenger_manifest FROM analysis.strategy_manifest
            WHERE strategy_revision_id = NEW.challenger_revision_id
              AND manifest_hash::TEXT = NEW.challenger_manifest_hash::TEXT;
            SELECT input_cutoff INTO champion_trial_cutoff FROM analysis.research_trial
            WHERE id = NEW.champion_trial_id;
            SELECT input_cutoff INTO challenger_trial_cutoff FROM analysis.research_trial
            WHERE id = NEW.challenger_trial_id;
            IF champion_result IS NULL OR challenger_result IS NULL
               OR champion_manifest IS NULL OR challenger_manifest IS NULL
               OR champion_trial_cutoff IS NULL OR challenger_trial_cutoff IS NULL
               OR NEW.input_cutoff > champion_trial_cutoff OR NEW.input_cutoff > challenger_trial_cutoff
               OR NEW.available_at > NEW.input_cutoff THEN
                RAISE EXCEPTION 'Phase 3 comparison has invalid canonical trial, manifest, or PIT lineage';
            END IF;
            SELECT count(*), corr(champion.net_return, challenger.net_return),
                   COALESCE(jsonb_agg(to_jsonb(champion.input_hash::TEXT) ORDER BY champion.id), '[]'::jsonb),
                   COALESCE(jsonb_agg(to_jsonb(challenger.input_hash::TEXT) ORDER BY challenger.id), '[]'::jsonb)
              INTO pair_count, return_correlation, champion_hashes, challenger_hashes
              FROM analysis.strategy_pnl_tape champion
              JOIN analysis.strategy_pnl_tape challenger
                ON challenger.instrument_id = champion.instrument_id
               AND challenger.pnl_date = champion.pnl_date
             WHERE champion.strategy_revision_id = NEW.champion_revision_id
               AND challenger.strategy_revision_id = NEW.challenger_revision_id
               AND champion.research_trial_id = NEW.champion_trial_id
               AND challenger.research_trial_id = NEW.challenger_trial_id
               AND champion.universe_manifest_hash::TEXT = NEW.champion_manifest_hash::TEXT
               AND challenger.universe_manifest_hash::TEXT = NEW.challenger_manifest_hash::TEXT
               AND champion.result_hash::TEXT = NEW.champion_result_hash::TEXT
               AND challenger.result_hash::TEXT = NEW.challenger_result_hash::TEXT
               AND champion.available_at <= NEW.input_cutoff
               AND challenger.available_at <= NEW.input_cutoff;
            SELECT promotability INTO challenger_class FROM analysis.strategy_revision
            WHERE id = NEW.challenger_revision_id;
            computed_distinctness := CASE
                WHEN challenger_class = 'exposure_sleeve' THEN 'exposure_sleeve'
                WHEN pair_count < 2 OR return_correlation IS NULL THEN 'inconclusive'
                WHEN abs(return_correlation) >= 0.8 THEN 'replica'
                ELSE 'distinct'
            END;
            NEW.distinctness := computed_distinctness;
            NEW.explanation := 'PostgreSQL classification from linked P&L tape correlation';
            NEW.metrics := jsonb_build_object(
                'paired_pnl_count', pair_count,
                'return_correlation', return_correlation,
                'champion_pnl_input_hashes', champion_hashes,
                'challenger_pnl_input_hashes', challenger_hashes,
                'generated_by', 'postgresql'
            );
            NEW.input_hash := analysis.phase3_json_hash(jsonb_build_object(
                'champion_revision_id', NEW.champion_revision_id,
                'challenger_revision_id', NEW.challenger_revision_id,
                'champion_trial_id', NEW.champion_trial_id,
                'challenger_trial_id', NEW.challenger_trial_id,
                'champion_result_hash', NEW.champion_result_hash,
                'challenger_result_hash', NEW.challenger_result_hash,
                'champion_manifest_hash', NEW.champion_manifest_hash,
                'challenger_manifest_hash', NEW.challenger_manifest_hash,
                'input_cutoff', NEW.input_cutoff, 'distinctness', NEW.distinctness,
                'metrics', NEW.metrics
            ));
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_strategy_comparison_lineage
            BEFORE INSERT ON analysis.strategy_comparison
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_comparison();

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
        DECLARE evidence_count INTEGER; pnl_count INTEGER; forecast_count INTEGER;
                comparison_count INTEGER; expected_count INTEGER;
                promotion_trial UUID; promotion_result UUID; promotion_result_hash TEXT;
                promotion_manifest_hash TEXT; martingale_family BOOLEAN;
        BEGIN
            martingale_family := lower(coalesce(NEW.strategy_family, '')) = 'martingale'
                OR lower(coalesce(NEW.mechanism_class, '')) = 'martingale'
                OR lower(NEW.strategy_key) ~ '(^|_)martingale(_|$)'
                OR lower(coalesce(NEW.name, '')) ~ '(^|[^a-z])martingale([^a-z]|$)';
            IF martingale_family AND (
                NEW.status IN ('active', 'promoted')
                OR NEW.promotability <> 'negative_control'
                OR NEW.actionability <> 'research_only'
            ) THEN
                RAISE EXCEPTION 'Martingale strategy family is a permanent research-only negative control';
            END IF;
            IF NEW.p3_enabled AND NEW.status IN ('active', 'promoted') THEN
                IF NEW.promotability <> 'standard' THEN
                    RAISE EXCEPTION 'only standard Phase 3 strategies can be promoted';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM analysis.strategy_manifest manifest WHERE manifest.strategy_revision_id = NEW.id) THEN
                    RAISE EXCEPTION 'Phase 3 strategy promotion requires an immutable strategy manifest';
                END IF;
                SELECT trial.id, result.id, result.input_hash::TEXT, manifest.manifest_hash::TEXT,
                       manifest.expected_member_count
                  INTO promotion_trial, promotion_result, promotion_result_hash,
                       promotion_manifest_hash, expected_count
                  FROM analysis.validation_dossier dossier
                  JOIN analysis.research_trial trial ON trial.id = dossier.research_trial_id
                  JOIN analysis.trial_result result
                    ON result.research_trial_id = trial.id
                   AND result.result_kind = 'validation'
                   AND result.outcome->>'passed' = 'true'
                  JOIN analysis.trial_universe_manifest manifest ON manifest.research_trial_id = trial.id
                 WHERE dossier.strategy_revision_id = NEW.id
                   AND dossier.status = 'sealed'
                   AND trial.status = 'succeeded'
                   AND analysis.research_trial_p3_denominator_complete(trial.id)
                   AND manifest.available_at <= trial.input_cutoff
                 ORDER BY result.available_at DESC
                 LIMIT 1;
                IF promotion_trial IS NULL THEN
                    RAISE EXCEPTION 'Phase 3 strategy promotion requires complete PIT denominator outcomes';
                END IF;
                SELECT count(*), count(DISTINCT strategy_forecast_id)
                  INTO pnl_count, forecast_count
                  FROM analysis.strategy_pnl_tape
                 WHERE strategy_revision_id = NEW.id
                   AND research_trial_id = promotion_trial
                   AND trial_result_id = promotion_result
                   AND result_hash::TEXT = promotion_result_hash
                   AND universe_manifest_hash::TEXT = promotion_manifest_hash
                   AND available_at <= input_cutoff;
                IF pnl_count <> expected_count OR forecast_count <> expected_count THEN
                    RAISE EXCEPTION 'Phase 3 promotion requires complete canonical P&L and forecast linkage';
                END IF;
                SELECT count(DISTINCT evidence_kind) INTO evidence_count
                FROM analysis.strategy_monitoring_evidence
                WHERE strategy_revision_id = NEW.id
                  AND research_trial_id = promotion_trial
                  AND trial_result_id = promotion_result
                  AND result_hash::TEXT = promotion_result_hash
                  AND universe_manifest_hash::TEXT = promotion_manifest_hash
                  AND available_at <= input_cutoff;
                IF evidence_count <> 6 THEN
                    RAISE EXCEPTION 'Phase 3 active strategy requires correlation, tail, crowding, capacity, decay, and regime evidence';
                END IF;
                SELECT count(*) INTO comparison_count
                FROM analysis.strategy_comparison comparison
                WHERE ((comparison.champion_revision_id = NEW.id
                        AND comparison.champion_trial_id = promotion_trial
                        AND comparison.champion_result_hash = promotion_result_hash
                        AND comparison.champion_manifest_hash = promotion_manifest_hash)
                    OR (comparison.challenger_revision_id = NEW.id
                        AND comparison.challenger_trial_id = promotion_trial
                        AND comparison.challenger_result_hash = promotion_result_hash
                        AND comparison.challenger_manifest_hash = promotion_manifest_hash))
                  AND comparison.distinctness IN ('distinct', 'exposure_sleeve')
                  AND comparison.available_at <= comparison.input_cutoff;
                IF comparison_count = 0 THEN
                    RAISE EXCEPTION 'Phase 3 promotion requires canonical champion/challenger evidence';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_phase3_strategy_status
            BEFORE INSERT OR UPDATE OF status, promotability, actionability, strategy_family,
                                      strategy_key, mechanism_class, p3_enabled ON analysis.strategy_revision
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_strategy_status();

        CREATE VIEW analysis.strategy_registry AS
        SELECT revision.id AS strategy_revision_id, revision.strategy_key, revision.revision,
               revision.name, revision.status, revision.mechanism_class,
               revision.economic_mechanism, revision.falsification_rule,
               revision.source_definition_version, revision.strategy_family, revision.promotability,
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
        REVOKE SELECT ON analysis.strategy_registry, analysis.strategy_trial_accounting
          FROM market_app;
        REVOKE SELECT ON analysis.strategy_manifest, analysis.strategy_pnl_tape,
                          analysis.strategy_monitoring_evidence, analysis.strategy_comparison
          FROM market_app;
        REVOKE INSERT ON analysis.strategy_manifest, analysis.strategy_pnl_tape,
                           analysis.strategy_monitoring_evidence, analysis.strategy_comparison
          FROM market_app;
        DROP VIEW IF EXISTS analysis.strategy_trial_accounting;
        DROP VIEW IF EXISTS analysis.strategy_registry;
        DROP TRIGGER IF EXISTS enforce_phase3_strategy_status ON analysis.strategy_revision;
        DROP FUNCTION IF EXISTS analysis.enforce_phase3_strategy_status();
        DROP TRIGGER IF EXISTS enforce_strategy_comparison_lineage ON analysis.strategy_comparison;
        DROP TRIGGER IF EXISTS enforce_strategy_comparison_immutable ON analysis.strategy_comparison;
        DROP TRIGGER IF EXISTS enforce_strategy_monitoring_lineage ON analysis.strategy_monitoring_evidence;
        DROP TRIGGER IF EXISTS enforce_strategy_monitoring_immutable ON analysis.strategy_monitoring_evidence;
        DROP TRIGGER IF EXISTS enforce_strategy_pnl_tape_lineage ON analysis.strategy_pnl_tape;
        DROP TRIGGER IF EXISTS enforce_strategy_pnl_tape_immutable ON analysis.strategy_pnl_tape;
        DROP TRIGGER IF EXISTS enforce_strategy_forecast_phase3_link ON analysis.strategy_forecast;
        DROP TRIGGER IF EXISTS enforce_strategy_manifest_hash ON analysis.strategy_manifest;
        DROP TRIGGER IF EXISTS enforce_strategy_manifest_immutable ON analysis.strategy_manifest;
        DROP FUNCTION IF EXISTS analysis.enforce_phase3_comparison();
        DROP FUNCTION IF EXISTS analysis.enforce_phase3_monitoring();
        DROP FUNCTION IF EXISTS analysis.enforce_phase3_pnl_tape();
        DROP FUNCTION IF EXISTS analysis.enforce_phase3_forecast_link();
        DROP FUNCTION IF EXISTS analysis.enforce_phase3_manifest_hash();
        DROP FUNCTION IF EXISTS analysis.enforce_phase3_immutable();
        DROP FUNCTION IF EXISTS analysis.research_trial_p3_denominator_complete(UUID);
        DROP FUNCTION IF EXISTS analysis.phase3_json_hash(JSONB);
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
            DROP COLUMN IF EXISTS mechanism_class,
            DROP COLUMN IF EXISTS strategy_family;
        ALTER TABLE analysis.strategy_forecast
            DROP CONSTRAINT IF EXISTS strategy_forecast_p3_pit_check,
            DROP CONSTRAINT IF EXISTS strategy_forecast_p3_link_check,
            DROP COLUMN IF EXISTS result_hash,
            DROP COLUMN IF EXISTS universe_manifest_hash,
            DROP COLUMN IF EXISTS trial_result_id,
            DROP COLUMN IF EXISTS research_trial_id;
        """
    )
