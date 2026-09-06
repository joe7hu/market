-- Verified PostgreSQL 18 baseline. Edit through a future forward migration.
SET LOCAL check_function_bodies = false;
CREATE SCHEMA analysis;

CREATE SCHEMA app;

CREATE SCHEMA catalog;

CREATE SCHEMA ingest;

CREATE SCHEMA ops;

CREATE SCHEMA raw;

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;

CREATE FUNCTION analysis.assign_current_option_recovery_cohort() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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

CREATE FUNCTION analysis.assign_research_gate_actual_clock() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            IF NEW.evaluated_at IS NOT NULL OR NEW.available_at IS NOT NULL THEN
                RAISE EXCEPTION 'validation gate timestamps are database-owned';
            END IF;
            NEW.evaluated_at := actual;
            NEW.available_at := actual;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.canonical_forecast_number(value double precision) RETURNS text
    LANGUAGE plpgsql IMMUTABLE
    AS $$
        DECLARE text_value TEXT;
        BEGIN
            IF value IS NULL THEN RETURN NULL; END IF;
            IF value = 0 THEN RETURN '0'; END IF;
            IF value <> value OR value = 'Infinity'::DOUBLE PRECISION OR value = '-Infinity'::DOUBLE PRECISION THEN
                RAISE EXCEPTION 'forecast numeric payload must be finite';
            END IF;
            text_value := to_char(value::NUMERIC, 'FM999999999999999999999999999999999999990D999999999999999999999999999999999999999');
            text_value := rtrim(rtrim(text_value, '0'), '.');
            RETURN CASE WHEN text_value IN ('', '-0') THEN '0' ELSE text_value END;
        END;
        $$;

CREATE FUNCTION analysis.enforce_phase3_comparison() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE champion_result TEXT; challenger_result TEXT; champion_manifest TEXT; challenger_manifest TEXT;
              champion_trial_cutoff TIMESTAMPTZ; challenger_trial_cutoff TIMESTAMPTZ;
              champion_result_available TIMESTAMPTZ; challenger_result_available TIMESTAMPTZ;
              pair_count INTEGER; return_correlation DOUBLE PRECISION; computed_distinctness TEXT;
              champion_hashes JSONB; challenger_hashes JSONB; challenger_class TEXT;
              champion_outcome JSONB; challenger_outcome JSONB; factor_evidence_valid BOOLEAN;
        BEGIN
            SELECT input_hash::TEXT, available_at, outcome INTO champion_result, champion_result_available, champion_outcome FROM analysis.trial_result
            WHERE id = NEW.champion_result_id AND research_trial_id = NEW.champion_trial_id
              AND input_hash::TEXT = NEW.champion_result_hash::TEXT;
            SELECT input_hash::TEXT, available_at, outcome INTO challenger_result, challenger_result_available, challenger_outcome FROM analysis.trial_result
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
               OR champion_result_available IS NULL OR challenger_result_available IS NULL
               OR NEW.input_cutoff > champion_trial_cutoff OR NEW.input_cutoff > challenger_trial_cutoff
               OR champion_result_available > NEW.input_cutoff OR challenger_result_available > NEW.input_cutoff
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
            factor_evidence_valid := analysis.phase3_factor_evidence_valid(
                NEW.champion_result_id, champion_result, NEW.input_cutoff
            ) AND analysis.phase3_factor_evidence_valid(
                NEW.challenger_result_id, challenger_result, NEW.input_cutoff
            );
            computed_distinctness := CASE
                WHEN NOT factor_evidence_valid THEN 'blocked'
                WHEN challenger_class = 'exposure_sleeve' THEN 'exposure_sleeve'
                WHEN pair_count < 2 OR return_correlation IS NULL THEN 'inconclusive'
                WHEN abs(return_correlation) >= 0.8 THEN 'replica'
                ELSE 'distinct'
            END;
            NEW.distinctness := computed_distinctness;
            NEW.explanation := CASE WHEN factor_evidence_valid
                THEN 'PostgreSQL classification from linked factor evidence and P&L tape'
                ELSE 'blocked: canonical factor exposure and neutralized-result lineage are absent or invalid'
            END;
            NEW.metrics := jsonb_build_object(
                'paired_pnl_count', pair_count,
                'return_correlation', return_correlation,
                'champion_pnl_input_hashes', champion_hashes,
                'challenger_pnl_input_hashes', challenger_hashes,
                'factor_evidence_valid', factor_evidence_valid,
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

CREATE FUNCTION analysis.enforce_phase3_forecast_link() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE trial_input_hash TEXT; result_input_hash TEXT;
              trial_input_cutoff TIMESTAMPTZ; result_available_at TIMESTAMPTZ;
              manifest_digest TEXT;
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
            SELECT input_cutoff INTO trial_input_cutoff
            FROM analysis.research_trial WHERE id = NEW.research_trial_id;
            SELECT available_at INTO result_available_at
            FROM analysis.trial_result
            WHERE id = NEW.trial_result_id AND research_trial_id = NEW.research_trial_id;
            SELECT manifest_hash::TEXT INTO manifest_digest
            FROM analysis.strategy_manifest
            WHERE strategy_revision_id = NEW.strategy_revision_id;
            IF trial_input_hash IS NULL OR result_input_hash IS NULL
               OR manifest_digest IS NULL
               OR NEW.universe_manifest_hash::TEXT <> manifest_digest
               OR NEW.result_hash::TEXT <> result_input_hash
               OR trial_input_cutoff IS NULL OR NEW.input_cutoff > trial_input_cutoff
               OR result_available_at IS NULL OR result_available_at > trial_input_cutoff
               OR result_available_at > NEW.input_cutoff
               OR NEW.available_at > NEW.input_cutoff THEN
                RAISE EXCEPTION 'Phase 3 forecast lineage or PIT clock is invalid';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_phase3_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'Phase 3 research evidence is immutable';
        END;
        $$;

CREATE FUNCTION analysis.enforce_phase3_manifest_hash() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            NEW.manifest_hash := analysis.phase3_json_hash(jsonb_build_object(
                'source', NEW.source_manifest, 'data', NEW.data_manifest,
                'cost', NEW.cost_manifest, 'capacity', NEW.capacity_manifest,
                'failure', NEW.failure_manifest
            ));
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_phase3_monitoring() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE result_input_hash TEXT; result_available_at TIMESTAMPTZ;
              manifest_digest TEXT; trial_input_cutoff TIMESTAMPTZ;
              tape_count BIGINT; forecast_count BIGINT; instrument_count BIGINT;
              tail_count BIGINT; regime_count BIGINT; date_count BIGINT;
              gross_abs DOUBLE PRECISION; cost_abs DOUBLE PRECISION;
              return_correlation DOUBLE PRECISION; downside_correlation DOUBLE PRECISION;
              tail_correlation DOUBLE PRECISION; crowding_hhi DOUBLE PRECISION;
              intended_notional DOUBLE PRECISION; unwind_cost DOUBLE PRECISION;
              capacity_cost_ratio DOUBLE PRECISION; decay_slope DOUBLE PRECISION;
              regime_dispersion DOUBLE PRECISION; regime_results JSONB;
              tape_hashes JSONB; forecast_hashes JSONB; linked_revision_ids JSONB;
              peer_revision_id BIGINT; peer_pnl_hashes JSONB; peer_result_hashes JSONB;
              paired_count BIGINT; downside_pair_count BIGINT; tail_pair_count BIGINT;
              effective_sample_size DOUBLE PRECISION; tail_effective_sample_size DOUBLE PRECISION;
              expected_effective_sample_size DOUBLE PRECISION;
              provided_metrics JSONB; provided_evidence JSONB;
              metric_name TEXT; metric_value DOUBLE PRECISION;
              expected_metric DOUBLE PRECISION; expected_sample BIGINT;
              provided_sample BIGINT;
        BEGIN
            provided_metrics := COALESCE(NEW.metrics, '{}'::jsonb);
            provided_evidence := COALESCE(NEW.evidence, '{}'::jsonb);
            linked_revision_ids := provided_evidence->'linked_strategy_revision_ids';
            IF jsonb_typeof(provided_evidence) IS DISTINCT FROM 'object'
               OR jsonb_typeof(linked_revision_ids) IS DISTINCT FROM 'array'
               OR jsonb_array_length(linked_revision_ids) < 2
               OR EXISTS (
                   SELECT 1 FROM jsonb_array_elements(linked_revision_ids) linked
                   WHERE jsonb_typeof(linked.value) IS DISTINCT FROM 'number'
               )
               OR (linked_revision_ids->>0)::BIGINT <> NEW.strategy_revision_id THEN
                RAISE EXCEPTION 'Phase 3 monitoring evidence requires linked strategy revisions';
            END IF;
            peer_revision_id := (linked_revision_ids->>1)::BIGINT;
            IF peer_revision_id = NEW.strategy_revision_id
               OR NOT EXISTS (SELECT 1 FROM analysis.strategy_revision WHERE id = peer_revision_id) THEN
                RAISE EXCEPTION 'Phase 3 monitoring evidence requires a distinct linked strategy revision';
            END IF;
            SELECT input_hash::TEXT, available_at INTO result_input_hash, result_available_at FROM analysis.trial_result
            WHERE id = NEW.trial_result_id AND research_trial_id = NEW.research_trial_id;
            SELECT manifest_hash::TEXT INTO manifest_digest FROM analysis.strategy_manifest
            WHERE strategy_revision_id = NEW.strategy_revision_id;
            SELECT input_cutoff INTO trial_input_cutoff FROM analysis.research_trial
            WHERE id = NEW.research_trial_id;
            IF result_input_hash IS NULL OR result_available_at IS NULL OR manifest_digest IS NULL
               OR NEW.result_hash::TEXT <> result_input_hash
               OR NEW.universe_manifest_hash::TEXT <> manifest_digest
               OR trial_input_cutoff IS NULL OR NEW.input_cutoff > trial_input_cutoff
               OR result_available_at > trial_input_cutoff
               OR result_available_at > NEW.input_cutoff
               OR NEW.available_at > NEW.input_cutoff THEN
                RAISE EXCEPTION 'Phase 3 monitoring evidence has invalid trial, manifest, or PIT lineage';
            END IF;
            SELECT count(*), count(DISTINCT tape.instrument_id),
                   count(*) FILTER (WHERE tape.tail_return IS NOT NULL),
                   count(DISTINCT tape.regime) FILTER (WHERE tape.regime IS NOT NULL),
                   count(forecast.id), sum(abs(tape.gross_return)), sum(abs(tape.cost)),
                   COALESCE(jsonb_agg(to_jsonb(tape.input_hash::TEXT) ORDER BY tape.id), '[]'::jsonb),
                   COALESCE(jsonb_agg(to_jsonb(forecast.input_hash::TEXT) ORDER BY tape.id), '[]'::jsonb),
                   count(DISTINCT tape.pnl_date)
              INTO tape_count, instrument_count, tail_count, regime_count, forecast_count,
                   gross_abs, cost_abs, tape_hashes, forecast_hashes, date_count
              FROM analysis.strategy_pnl_tape tape
              JOIN analysis.strategy_forecast forecast
                ON forecast.id = tape.strategy_forecast_id
               AND forecast.research_trial_id = tape.research_trial_id
               AND forecast.trial_result_id = tape.trial_result_id
               AND forecast.universe_manifest_hash::TEXT = tape.universe_manifest_hash::TEXT
               AND forecast.result_hash::TEXT = tape.result_hash::TEXT
             WHERE tape.strategy_revision_id = NEW.strategy_revision_id
               AND tape.research_trial_id = NEW.research_trial_id
               AND tape.trial_result_id = NEW.trial_result_id
               AND tape.universe_manifest_hash::TEXT = NEW.universe_manifest_hash::TEXT
               AND tape.result_hash::TEXT = NEW.result_hash::TEXT
               AND tape.available_at <= NEW.input_cutoff
               AND forecast.available_at <= NEW.input_cutoff;
            SELECT COALESCE(sum(power(weight, 2)), NULL) INTO crowding_hhi
            FROM (
                SELECT abs(tape.net_return) / NULLIF(sum(abs(tape.net_return)) OVER (), 0) AS weight
                  FROM analysis.strategy_pnl_tape tape
                  JOIN analysis.strategy_forecast forecast ON forecast.id = tape.strategy_forecast_id
                 WHERE tape.strategy_revision_id = NEW.strategy_revision_id
                   AND tape.research_trial_id = NEW.research_trial_id
                   AND tape.trial_result_id = NEW.trial_result_id
                   AND tape.universe_manifest_hash::TEXT = NEW.universe_manifest_hash::TEXT
                   AND tape.result_hash::TEXT = NEW.result_hash::TEXT
                   AND tape.available_at <= NEW.input_cutoff
                   AND forecast.available_at <= NEW.input_cutoff
            ) weighted;
            SELECT count(*),
                   corr(base.net_return, peer.net_return),
                   corr(base.net_return, peer.net_return)
                     FILTER (WHERE base.net_return < 0 AND peer.net_return < 0),
                   corr(base.tail_return, peer.tail_return) FILTER (WHERE base.tail_return IS NOT NULL AND peer.tail_return IS NOT NULL),
                   power(sum(abs(base.net_return) + abs(peer.net_return)), 2)
                     / NULLIF(sum(power(abs(base.net_return) + abs(peer.net_return), 2)), 0),
                   power(sum(abs(base.tail_return) + abs(peer.tail_return)) FILTER (WHERE base.tail_return IS NOT NULL AND peer.tail_return IS NOT NULL), 2)
                     / NULLIF(sum(power(abs(base.tail_return) + abs(peer.tail_return), 2)) FILTER (WHERE base.tail_return IS NOT NULL AND peer.tail_return IS NOT NULL), 0),
                   count(*) FILTER (WHERE base.net_return < 0 OR peer.net_return < 0),
                   count(*) FILTER (WHERE base.tail_return IS NOT NULL AND peer.tail_return IS NOT NULL),
                   COALESCE(jsonb_agg(to_jsonb(peer.input_hash::TEXT) ORDER BY peer.id), '[]'::jsonb)
              INTO paired_count, return_correlation, downside_correlation, tail_correlation,
                   effective_sample_size, tail_effective_sample_size, downside_pair_count,
                   tail_pair_count, peer_pnl_hashes
              FROM analysis.strategy_pnl_tape base
              JOIN analysis.strategy_pnl_tape peer
                ON peer.strategy_revision_id = peer_revision_id
               AND peer.instrument_id = base.instrument_id
               AND peer.pnl_date = base.pnl_date
               AND peer.available_at <= NEW.input_cutoff
             WHERE base.strategy_revision_id = NEW.strategy_revision_id
               AND base.research_trial_id = NEW.research_trial_id
               AND base.trial_result_id = NEW.trial_result_id
               AND base.universe_manifest_hash::TEXT = NEW.universe_manifest_hash::TEXT
               AND base.result_hash::TEXT = NEW.result_hash::TEXT
               AND base.available_at <= NEW.input_cutoff;
            SELECT COALESCE(jsonb_agg(result_hash ORDER BY result_hash), '[]'::jsonb)
              INTO peer_result_hashes
              FROM (
                  SELECT DISTINCT peer.result_hash::TEXT AS result_hash
                    FROM analysis.strategy_pnl_tape peer
                   WHERE peer.strategy_revision_id = peer_revision_id
                     AND peer.available_at <= NEW.input_cutoff
              ) peer_results;
            SELECT sum((tape.metadata->>'intended_notional')::DOUBLE PRECISION),
                   sum((tape.metadata->>'unwind_cost')::DOUBLE PRECISION)
              INTO intended_notional, unwind_cost
              FROM analysis.strategy_pnl_tape tape
             WHERE tape.strategy_revision_id = NEW.strategy_revision_id
               AND tape.research_trial_id = NEW.research_trial_id
               AND tape.trial_result_id = NEW.trial_result_id
               AND tape.universe_manifest_hash::TEXT = NEW.universe_manifest_hash::TEXT
               AND tape.result_hash::TEXT = NEW.result_hash::TEXT
               AND tape.available_at <= NEW.input_cutoff;
            SELECT regr_slope(tape.net_return, extract(epoch FROM tape.pnl_date::timestamp))
              INTO decay_slope
              FROM analysis.strategy_pnl_tape tape
             WHERE tape.strategy_revision_id = NEW.strategy_revision_id
               AND tape.research_trial_id = NEW.research_trial_id
               AND tape.trial_result_id = NEW.trial_result_id
               AND tape.universe_manifest_hash::TEXT = NEW.universe_manifest_hash::TEXT
               AND tape.result_hash::TEXT = NEW.result_hash::TEXT
               AND tape.available_at <= NEW.input_cutoff;
            SELECT COALESCE(jsonb_object_agg(regime, jsonb_build_object(
                       'net_return', avg_return,
                       'sample_size', sample_count,
                       'effective_sample_size', regime_effective_sample_size
                   )), '{}'::jsonb)
              INTO regime_results
              FROM (
                  SELECT tape.regime, avg(tape.net_return) AS avg_return,
                         count(*) AS sample_count,
                         power(sum(abs(tape.net_return)), 2)
                           / NULLIF(sum(power(abs(tape.net_return), 2)), 0)
                           AS regime_effective_sample_size
                    FROM analysis.strategy_pnl_tape tape
                   WHERE tape.strategy_revision_id = NEW.strategy_revision_id
                     AND tape.research_trial_id = NEW.research_trial_id
                     AND tape.trial_result_id = NEW.trial_result_id
                     AND tape.universe_manifest_hash::TEXT = NEW.universe_manifest_hash::TEXT
                     AND tape.result_hash::TEXT = NEW.result_hash::TEXT
                     AND tape.available_at <= NEW.input_cutoff
                     AND tape.regime IS NOT NULL
                   GROUP BY tape.regime
              ) by_regime;
            capacity_cost_ratio := CASE WHEN intended_notional > 0 THEN (cost_abs + unwind_cost) / intended_notional END;
            SELECT stddev_pop(avg_return) INTO regime_dispersion
              FROM (
                  SELECT tape.regime, avg(tape.net_return) AS avg_return
                    FROM analysis.strategy_pnl_tape tape
                   WHERE tape.strategy_revision_id = NEW.strategy_revision_id
                     AND tape.research_trial_id = NEW.research_trial_id
                     AND tape.trial_result_id = NEW.trial_result_id
                     AND tape.universe_manifest_hash::TEXT = NEW.universe_manifest_hash::TEXT
                     AND tape.result_hash::TEXT = NEW.result_hash::TEXT
                     AND tape.available_at <= NEW.input_cutoff
                     AND tape.regime IS NOT NULL
                   GROUP BY tape.regime
              ) regime_means;
            IF tape_count = 0 OR forecast_count <> tape_count OR paired_count < 2
               OR downside_pair_count < 2 OR tail_pair_count < 2
               OR crowding_hhi IS NULL OR intended_notional IS NULL OR unwind_cost IS NULL
               OR capacity_cost_ratio IS NULL OR decay_slope IS NULL
               OR regime_results = '{}'::jsonb OR regime_dispersion IS NULL THEN
                RAISE EXCEPTION 'Phase 3 monitoring evidence requires canonical P&L tape lineage';
            END IF;
            metric_name := CASE NEW.evidence_kind
                WHEN 'correlation' THEN 'normal_correlation'
                WHEN 'tail_correlation' THEN 'tail_correlation'
                WHEN 'crowding' THEN 'crowding_hhi'
                WHEN 'capacity' THEN 'capacity_cost_ratio'
                WHEN 'decay' THEN 'decay_slope'
                WHEN 'regime' THEN 'regime_dispersion'
            END;
            expected_sample := CASE NEW.evidence_kind
                WHEN 'correlation' THEN paired_count
                WHEN 'tail_correlation' THEN tail_pair_count
                WHEN 'crowding' THEN instrument_count
                WHEN 'capacity' THEN tape_count
                WHEN 'decay' THEN date_count
                WHEN 'regime' THEN tape_count
            END;
            expected_metric := CASE NEW.evidence_kind
                WHEN 'correlation' THEN return_correlation
                WHEN 'tail_correlation' THEN tail_correlation
                WHEN 'crowding' THEN crowding_hhi
                WHEN 'capacity' THEN capacity_cost_ratio
                WHEN 'decay' THEN decay_slope
                WHEN 'regime' THEN regime_dispersion
            END;
            IF expected_sample < 2 OR expected_metric IS NULL THEN
                RAISE EXCEPTION 'Phase 3 monitoring evidence kind lacks required canonical metric';
            END IF;
            IF jsonb_typeof(provided_metrics) IS DISTINCT FROM 'object'
               OR jsonb_typeof(provided_evidence) IS DISTINCT FROM 'object'
               OR provided_metrics->>'evidence_kind' IS DISTINCT FROM NEW.evidence_kind
               OR provided_metrics->>'metric_name' IS DISTINCT FROM metric_name
               OR jsonb_typeof(provided_metrics->'sample_size') IS DISTINCT FROM 'number'
               OR jsonb_typeof(provided_metrics->metric_name) IS DISTINCT FROM 'number'
               OR provided_evidence->>'evidence_kind' IS DISTINCT FROM NEW.evidence_kind
               OR provided_evidence->>'metric_name' IS DISTINCT FROM metric_name
               OR jsonb_typeof(provided_evidence->'sample_size') IS DISTINCT FROM 'number'
               OR jsonb_typeof(provided_metrics->'effective_sample_size') IS DISTINCT FROM 'number'
               OR jsonb_typeof(provided_evidence->'effective_sample_size') IS DISTINCT FROM 'number'
               OR jsonb_typeof(provided_evidence->'metric_value') IS DISTINCT FROM 'number'
               OR provided_evidence->>'trial_result_hash' IS DISTINCT FROM NEW.result_hash::TEXT
               OR provided_evidence->>'universe_manifest_hash' IS DISTINCT FROM NEW.universe_manifest_hash::TEXT
               OR jsonb_typeof(provided_evidence->'pnl_input_hashes') IS DISTINCT FROM 'array'
               OR jsonb_typeof(provided_evidence->'forecast_input_hashes') IS DISTINCT FROM 'array'
               OR jsonb_typeof(provided_evidence->'linked_strategy_revision_ids') IS DISTINCT FROM 'array'
               OR jsonb_typeof(provided_evidence->'linked_pnl_input_hashes') IS DISTINCT FROM 'array'
               OR jsonb_typeof(provided_evidence->'linked_result_hashes') IS DISTINCT FROM 'array'
               OR jsonb_typeof(provided_evidence->'normal_correlation') IS DISTINCT FROM 'number'
               OR jsonb_typeof(provided_evidence->'downside_correlation') IS DISTINCT FROM 'number'
               OR jsonb_typeof(provided_evidence->'tail_correlation') IS DISTINCT FROM 'number'
               OR jsonb_typeof(provided_evidence->'intended_notional') IS DISTINCT FROM 'number'
               OR jsonb_typeof(provided_evidence->'unwind_cost') IS DISTINCT FROM 'number'
               OR jsonb_typeof(provided_evidence->'capacity_cost_ratio') IS DISTINCT FROM 'number'
               OR jsonb_typeof(provided_evidence->'decay_slope') IS DISTINCT FROM 'number'
               OR jsonb_typeof(provided_evidence->'regime_dispersion') IS DISTINCT FROM 'number'
               OR jsonb_typeof(provided_evidence->'regime_slices') IS DISTINCT FROM 'object'
               OR provided_evidence->>'input_cutoff' IS NULL
               OR provided_evidence->>'observed_at' IS NULL
               OR provided_evidence->>'available_at' IS NULL THEN
                RAISE EXCEPTION 'Phase 3 monitoring evidence requires typed canonical content and lineage';
            END IF;
            provided_sample := (provided_metrics->>'sample_size')::BIGINT;
            expected_effective_sample_size := CASE NEW.evidence_kind
                WHEN 'correlation' THEN effective_sample_size
                WHEN 'tail_correlation' THEN tail_effective_sample_size
                WHEN 'crowding' THEN 1 / NULLIF(crowding_hhi, 0)
                WHEN 'capacity' THEN tape_count
                WHEN 'decay' THEN date_count
                WHEN 'regime' THEN tape_count
            END;
            IF provided_sample < 2
               OR provided_sample <> expected_sample
               OR (provided_evidence->>'sample_size')::BIGINT <> expected_sample
               OR abs((provided_metrics->>'effective_sample_size')::DOUBLE PRECISION - expected_effective_sample_size) > 1e-12
               OR abs((provided_evidence->>'effective_sample_size')::DOUBLE PRECISION - expected_effective_sample_size) > 1e-12
               OR abs((provided_metrics->>metric_name)::DOUBLE PRECISION - expected_metric) > 1e-12
               OR abs((provided_evidence->>'metric_value')::DOUBLE PRECISION - expected_metric) > 1e-12
               OR provided_evidence->'pnl_input_hashes' IS DISTINCT FROM tape_hashes
               OR provided_evidence->'forecast_input_hashes' IS DISTINCT FROM forecast_hashes
               OR provided_evidence->'linked_strategy_revision_ids' IS DISTINCT FROM linked_revision_ids
               OR provided_evidence->'linked_pnl_input_hashes' IS DISTINCT FROM peer_pnl_hashes
               OR provided_evidence->'linked_result_hashes' IS DISTINCT FROM peer_result_hashes
               OR provided_evidence->'normal_correlation' IS DISTINCT FROM to_jsonb(return_correlation)
               OR provided_evidence->'downside_correlation' IS DISTINCT FROM to_jsonb(downside_correlation)
               OR provided_evidence->'tail_correlation' IS DISTINCT FROM to_jsonb(tail_correlation)
               OR abs((provided_evidence->>'intended_notional')::DOUBLE PRECISION - intended_notional) > 1e-12
               OR abs((provided_evidence->>'unwind_cost')::DOUBLE PRECISION - unwind_cost) > 1e-12
               OR abs((provided_evidence->>'capacity_cost_ratio')::DOUBLE PRECISION - capacity_cost_ratio) > 1e-12
               OR abs((provided_evidence->>'decay_slope')::DOUBLE PRECISION - decay_slope) > 1e-12
               OR abs((provided_evidence->>'regime_dispersion')::DOUBLE PRECISION - regime_dispersion) > 1e-12
               OR provided_evidence->'regime_slices' IS DISTINCT FROM regime_results
               OR (provided_evidence->>'input_cutoff')::TIMESTAMPTZ IS DISTINCT FROM NEW.input_cutoff
               OR (provided_evidence->>'observed_at')::TIMESTAMPTZ IS DISTINCT FROM NEW.observed_at
               OR (provided_evidence->>'available_at')::TIMESTAMPTZ IS DISTINCT FROM NEW.available_at THEN
                RAISE EXCEPTION 'Phase 3 monitoring evidence content or lineage does not match canonical PostgreSQL evidence';
            END IF;
            NEW.lineage := jsonb_build_object(
                'research_trial_id', NEW.research_trial_id,
                'trial_result_id', NEW.trial_result_id,
                'universe_manifest_hash', NEW.universe_manifest_hash,
                'result_hash', NEW.result_hash,
                'forecast_observation_count', forecast_count,
                'pnl_input_hashes', tape_hashes,
                'forecast_input_hashes', forecast_hashes,
                'input_cutoff', NEW.input_cutoff,
                'generated_by', 'postgresql'
            );
            NEW.metrics := jsonb_build_object(
                'evidence_kind', NEW.evidence_kind,
                'pnl_observation_count', tape_count,
                'instrument_count', instrument_count,
                'tail_observation_count', tail_count,
                'regime_count', regime_count,
                'sample_size', expected_sample,
                'effective_sample_size', expected_effective_sample_size,
                'metric_name', metric_name,
                metric_name, expected_metric,
                'normal_correlation', return_correlation,
                'downside_correlation', downside_correlation,
                'tail_correlation', tail_correlation,
                'crowding_hhi', crowding_hhi,
                'intended_notional', intended_notional,
                'unwind_cost', unwind_cost,
                'capacity_cost_ratio', capacity_cost_ratio,
                'decay_slope', decay_slope,
                'regime_dispersion', regime_dispersion,
                'regime_slices', regime_results,
                'linked_strategy_revision_ids', linked_revision_ids,
                'linked_pnl_input_hashes', peer_pnl_hashes,
                'linked_result_hashes', peer_result_hashes,
                'generated_by', 'postgresql'
            );
            NEW.evidence := jsonb_build_object(
                'evidence_kind', NEW.evidence_kind,
                'metric_name', metric_name,
                'metric_value', expected_metric,
                'sample_size', expected_sample,
                'effective_sample_size', expected_effective_sample_size,
                'trial_result_hash', NEW.result_hash,
                'universe_manifest_hash', NEW.universe_manifest_hash,
                'pnl_input_hashes', tape_hashes,
                'forecast_input_hashes', forecast_hashes,
                'normal_correlation', return_correlation,
                'downside_correlation', downside_correlation,
                'tail_correlation', tail_correlation,
                'crowding_hhi', crowding_hhi,
                'intended_notional', intended_notional,
                'unwind_cost', unwind_cost,
                'capacity_cost_ratio', capacity_cost_ratio,
                'decay_slope', decay_slope,
                'regime_dispersion', regime_dispersion,
                'regime_slices', regime_results,
                'linked_strategy_revision_ids', linked_revision_ids,
                'linked_pnl_input_hashes', peer_pnl_hashes,
                'linked_result_hashes', peer_result_hashes,
                'input_cutoff', NEW.input_cutoff,
                'observed_at', NEW.observed_at,
                'available_at', NEW.available_at,
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

CREATE FUNCTION analysis.enforce_phase3_pnl_tape() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE forecast analysis.strategy_forecast%ROWTYPE;
              result_input_hash TEXT; result_available_at TIMESTAMPTZ;
              manifest_digest TEXT; trial_input_cutoff TIMESTAMPTZ;
        BEGIN
            SELECT * INTO forecast FROM analysis.strategy_forecast
            WHERE id = NEW.strategy_forecast_id;
            SELECT input_cutoff INTO trial_input_cutoff FROM analysis.research_trial
            WHERE id = NEW.research_trial_id;
            SELECT input_hash::TEXT, available_at INTO result_input_hash, result_available_at FROM analysis.trial_result
            WHERE id = NEW.trial_result_id AND research_trial_id = NEW.research_trial_id;
            SELECT manifest_hash::TEXT INTO manifest_digest FROM analysis.strategy_manifest
            WHERE strategy_revision_id = NEW.strategy_revision_id;
            IF jsonb_typeof(NEW.metadata) IS DISTINCT FROM 'object'
               OR jsonb_typeof(NEW.metadata->'intended_notional') IS DISTINCT FROM 'number'
               OR jsonb_typeof(NEW.metadata->'unwind_cost') IS DISTINCT FROM 'number'
               OR (NEW.metadata->>'intended_notional')::DOUBLE PRECISION <= 0
               OR (NEW.metadata->>'unwind_cost')::DOUBLE PRECISION < 0 THEN
                RAISE EXCEPTION 'Phase 3 P&L tape requires typed intended notional and unwind cost evidence';
            END IF;
            IF forecast.id IS NULL OR forecast.strategy_revision_id <> NEW.strategy_revision_id
               OR forecast.instrument_id <> NEW.instrument_id
               OR forecast.research_trial_id <> NEW.research_trial_id
               OR forecast.trial_result_id <> NEW.trial_result_id
               OR forecast.input_cutoff <> NEW.input_cutoff
               OR forecast.universe_manifest_hash::TEXT <> NEW.universe_manifest_hash::TEXT
               OR forecast.result_hash::TEXT <> NEW.result_hash::TEXT
               OR result_input_hash IS NULL OR result_available_at IS NULL OR manifest_digest IS NULL
               OR NEW.result_hash::TEXT <> result_input_hash
               OR NEW.universe_manifest_hash::TEXT <> manifest_digest
               OR trial_input_cutoff IS NULL OR NEW.input_cutoff > trial_input_cutoff
               OR result_available_at > trial_input_cutoff
               OR result_available_at > NEW.input_cutoff
               OR NEW.available_at > NEW.input_cutoff THEN
                RAISE EXCEPTION 'Phase 3 P&L tape has invalid canonical lineage or PIT clock';
            END IF;
            IF NOT analysis.research_trial_p3_denominator_complete(NEW.research_trial_id)
               OR NOT EXISTS (
                   SELECT 1 FROM analysis.universe_observation observation
                   WHERE observation.research_trial_id = NEW.research_trial_id
                     AND observation.instrument_id = NEW.instrument_id
                     AND observation.cutoff = NEW.input_cutoff
                     AND observation.input_hash = (SELECT input_hash FROM analysis.research_trial WHERE id = NEW.research_trial_id)
                     AND observation.outcome <> '{}'::jsonb
               ) THEN
                RAISE EXCEPTION 'Phase 3 P&L tape requires a complete canonical trial denominator';
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

CREATE FUNCTION analysis.enforce_phase3_strategy_status() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE evidence_count INTEGER; pnl_count INTEGER; forecast_count INTEGER;
                member_count INTEGER; invalid_member_count INTEGER;
                comparison_count INTEGER; expected_count INTEGER;
                promotion_trial UUID; promotion_result UUID; promotion_result_hash TEXT;
                promotion_manifest_hash TEXT; martingale_family BOOLEAN;
        BEGIN
            martingale_family := position('martingale' in lower(coalesce(NEW.strategy_family, ''))) > 0
                OR position('martingale' in lower(coalesce(NEW.mechanism_class, ''))) > 0
                OR position('martingale' in lower(coalesce(NEW.strategy_key, ''))) > 0
                OR position('martingale' in lower(coalesce(NEW.name, ''))) > 0;
            IF martingale_family AND (
                NEW.status IN ('active', 'promoted')
                OR NEW.promotability <> 'negative_control'
                OR NEW.actionability <> 'research_only'
            ) THEN
                RAISE EXCEPTION 'Martingale strategy family is a permanent research-only negative control';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.p3_enabled
               AND OLD.status IS DISTINCT FROM NEW.status
               AND current_setting('analysis.phase3_transition', true) IS DISTINCT FROM 'canonical' THEN
                RAISE EXCEPTION 'Phase 3 status changes require the canonical evidence-backed transition';
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
                   AND result.available_at <= trial.input_cutoff
                   AND analysis.research_trial_p3_denominator_complete(trial.id)
                   AND manifest.available_at <= trial.input_cutoff
                 ORDER BY result.available_at DESC
                 LIMIT 1;
                IF promotion_trial IS NULL THEN
                    RAISE EXCEPTION 'Phase 3 strategy promotion requires complete PIT denominator outcomes';
                END IF;
                SELECT count(*), count(DISTINCT strategy_forecast_id), count(DISTINCT instrument_id),
                       count(*) FILTER (WHERE NOT EXISTS (
                           SELECT 1 FROM analysis.universe_observation observation
                           WHERE observation.research_trial_id = strategy_pnl_tape.research_trial_id
                             AND observation.instrument_id = strategy_pnl_tape.instrument_id
                             AND observation.cutoff = strategy_pnl_tape.input_cutoff
                             AND observation.input_hash = (SELECT input_hash FROM analysis.research_trial WHERE id = strategy_pnl_tape.research_trial_id)
                             AND observation.outcome <> '{}'::jsonb
                       ))
                  INTO pnl_count, forecast_count, member_count, invalid_member_count
                  FROM analysis.strategy_pnl_tape
                 WHERE strategy_revision_id = NEW.id
                   AND research_trial_id = promotion_trial
                   AND trial_result_id = promotion_result
                   AND result_hash::TEXT = promotion_result_hash
                   AND universe_manifest_hash::TEXT = promotion_manifest_hash
                   AND available_at <= input_cutoff;
                IF pnl_count <> expected_count OR forecast_count <> expected_count
                   OR member_count <> expected_count OR invalid_member_count <> 0
                   OR EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements_text((SELECT expected_members FROM analysis.trial_universe_manifest WHERE research_trial_id = promotion_trial)) expected(member)
                        WHERE NOT EXISTS (
                            SELECT 1 FROM analysis.strategy_pnl_tape tape
                             WHERE tape.research_trial_id = promotion_trial
                               AND tape.strategy_revision_id = NEW.id
                               AND tape.instrument_id::TEXT = expected.member
                        )
                   )
                   OR EXISTS (
                       SELECT 1 FROM analysis.strategy_pnl_tape tape
                        WHERE tape.research_trial_id = promotion_trial
                          AND tape.strategy_revision_id = NEW.id
                          AND NOT EXISTS (
                              SELECT 1 FROM jsonb_array_elements_text((SELECT expected_members FROM analysis.trial_universe_manifest WHERE research_trial_id = promotion_trial)) expected(member)
                               WHERE expected.member = tape.instrument_id::TEXT
                          )
                   ) THEN
                    RAISE EXCEPTION 'Phase 3 promotion requires complete canonical P&L and forecast linkage';
                END IF;
                SELECT count(*) INTO evidence_count
                FROM (VALUES
                    ('correlation', 'normal_correlation'),
                    ('tail_correlation', 'tail_correlation'),
                    ('crowding', 'crowding_hhi'),
                    ('capacity', 'capacity_cost_ratio'),
                    ('decay', 'decay_slope'),
                    ('regime', 'regime_dispersion')
                ) required(evidence_kind, metric_name)
                WHERE EXISTS (
                    SELECT 1 FROM analysis.strategy_monitoring_evidence evidence
                    WHERE evidence.strategy_revision_id = NEW.id
                      AND evidence.research_trial_id = promotion_trial
                      AND evidence.trial_result_id = promotion_result
                      AND evidence.result_hash::TEXT = promotion_result_hash
                      AND evidence.universe_manifest_hash::TEXT = promotion_manifest_hash
                      AND evidence.evidence_kind = required.evidence_kind
                      AND evidence.input_cutoff <= (SELECT input_cutoff FROM analysis.research_trial WHERE id = promotion_trial)
                      AND evidence.available_at <= evidence.input_cutoff
                      AND (SELECT result.available_at FROM analysis.trial_result result WHERE result.id = promotion_result) <= evidence.input_cutoff
                      AND evidence.metrics->>'generated_by' = 'postgresql'
                      AND evidence.lineage->>'generated_by' = 'postgresql'
                      AND evidence.evidence->>'generated_by' = 'postgresql'
                      AND evidence.metrics->>'metric_name' = required.metric_name
                      AND evidence.evidence->>'metric_name' = required.metric_name
                      AND jsonb_typeof(evidence.metrics->required.metric_name) = 'number'
                      AND jsonb_typeof(evidence.evidence->'metric_value') = 'number'
                      AND jsonb_typeof(evidence.metrics->'sample_size') = 'number'
                      AND jsonb_typeof(evidence.metrics->'effective_sample_size') = 'number'
                      AND evidence.evidence->>'sample_size' = evidence.metrics->>'sample_size'
                      AND jsonb_typeof(evidence.evidence->'effective_sample_size') = 'number'
                      AND evidence.evidence->>'trial_result_hash' = promotion_result_hash
                      AND evidence.evidence->>'universe_manifest_hash' = promotion_manifest_hash
                      AND evidence.evidence->'pnl_input_hashes' = evidence.lineage->'pnl_input_hashes'
                      AND evidence.evidence->'forecast_input_hashes' = evidence.lineage->'forecast_input_hashes'
                      AND jsonb_typeof(evidence.evidence->'normal_correlation') = 'number'
                      AND jsonb_typeof(evidence.evidence->'downside_correlation') = 'number'
                      AND jsonb_typeof(evidence.evidence->'tail_correlation') = 'number'
                      AND jsonb_typeof(evidence.evidence->'intended_notional') = 'number'
                      AND jsonb_typeof(evidence.evidence->'unwind_cost') = 'number'
                      AND jsonb_typeof(evidence.evidence->'capacity_cost_ratio') = 'number'
                      AND jsonb_typeof(evidence.evidence->'decay_slope') = 'number'
                      AND jsonb_typeof(evidence.evidence->'regime_slices') = 'object'
                      AND jsonb_typeof(evidence.evidence->'linked_strategy_revision_ids') = 'array'
                      AND jsonb_typeof(evidence.evidence->'linked_pnl_input_hashes') = 'array'
                      AND jsonb_typeof(evidence.evidence->'linked_result_hashes') = 'array'
                      AND evidence.evidence->>'input_cutoff' IS NOT NULL
                      AND evidence.evidence->>'observed_at' IS NOT NULL
                      AND evidence.evidence->>'available_at' IS NOT NULL
                      AND (evidence.evidence->>'input_cutoff')::TIMESTAMPTZ = evidence.input_cutoff
                      AND (evidence.evidence->>'observed_at')::TIMESTAMPTZ = evidence.observed_at
                      AND (evidence.evidence->>'available_at')::TIMESTAMPTZ = evidence.available_at
                      AND evidence.evidence->'linked_strategy_revision_ids' <> '[]'::jsonb
                );
                IF evidence_count <> 6 THEN
                    RAISE EXCEPTION 'Phase 3 active strategy requires complete typed evidence for all monitoring dimensions';
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
                  AND comparison.input_cutoff <= (SELECT input_cutoff FROM analysis.research_trial WHERE id = promotion_trial)
                  AND comparison.available_at <= comparison.input_cutoff;
                IF comparison_count = 0 THEN
                    RAISE EXCEPTION 'Phase 3 promotion requires canonical champion/challenger evidence';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_phase4_allocation_item_funding_lineage() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$ DECLARE allocation_meta JSONB; allocation_cutoff TIMESTAMPTZ; BEGIN SELECT snapshot.metadata, snapshot.input_cutoff INTO allocation_meta, allocation_cutoff FROM analysis.portfolio_allocation_snapshot snapshot WHERE snapshot.allocation_id = NEW.allocation_id; IF NEW.disposition IN ('selected', 'rollback') AND NEW.ticker <> 'CASH' THEN IF allocation_meta IS NULL OR allocation_meta->>'authority' <> 'postgresql' OR allocation_meta->>'execution_status' <> 'calibrated' OR NEW.strategy_forecast_id IS NULL OR NEW.action_id IS NULL OR NEW.rank_id IS NULL OR NEW.trace->>'source_decision_id' IS NULL OR NEW.trace->>'source_decision_input_hash' IS NULL OR NOT analysis.phase4_account_authority_exists( allocation_meta->>'authority_snapshot_id', allocation_cutoff ) OR NOT EXISTS ( SELECT 1 FROM analysis.execution_model_snapshot model JOIN analysis.portfolio_allocation_snapshot model_allocation ON model_allocation.allocation_id = model.allocation_id WHERE model.execution_model_snapshot_id = allocation_meta->>'execution_model_snapshot_id' AND model.model_version = 'paper-telemetry.v1' AND model.calibration_status = 'calibrated' AND model.sample_count > 0 AND model.input_cutoff > model_allocation.input_cutoff AND model.input_cutoff <= allocation_cutoff AND model.available_at <= allocation_cutoff AND model.metadata->>'source' = 'paper_execution_observation' AND jsonb_typeof(model.metadata->'paper_observation_ids') = 'array' AND jsonb_array_length(model.metadata->'paper_observation_ids') = model.sample_count AND CASE WHEN model.metadata->>'genuine_fill_count' ~ '^[0-9]+$' THEN (model.metadata->>'genuine_fill_count')::INTEGER = model.sample_count ELSE false END ) OR NOT EXISTS ( SELECT 1 FROM analysis.ticker_decision decision JOIN analysis.strategy_forecast forecast ON forecast.id = NEW.strategy_forecast_id WHERE decision.id::TEXT = NEW.trace->>'source_decision_id' AND decision.input_hash = NEW.trace->>'source_decision_input_hash' AND decision.instrument_id = forecast.instrument_id AND decision.status = 'published' AND decision.published_at IS NOT NULL AND decision.as_of <= allocation_cutoff AND decision.input_manifest->'trade_plan'->>'trade_plan_id' = NEW.action_id AND decision.input_manifest->'trade_plan'->>'rank_id' = NEW.rank_id AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id AND forecast.status = 'available' AND forecast.available_at <= forecast.input_cutoff AND forecast.input_cutoff <= allocation_cutoff AND decision.published_at <= allocation_cutoff ) OR jsonb_typeof(allocation_meta->'source_hashes') IS DISTINCT FROM 'array' OR NOT EXISTS ( SELECT 1 FROM jsonb_array_elements_text(allocation_meta->'source_hashes') hash WHERE hash = NEW.trace->>'source_decision_input_hash' ) THEN RAISE EXCEPTION 'Phase 4 allocation authority is not bound to PostgreSQL action lineage'; END IF; END IF; NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'allocation_item_id', NEW.allocation_item_id, 'allocation_id', NEW.allocation_id, 'candidate_id', NEW.candidate_id, 'ticker', NEW.ticker, 'strategy_forecast_id', NEW.strategy_forecast_id, 'action_id', NEW.action_id, 'rank_id', NEW.rank_id, 'hypothesis_id', NEW.hypothesis_id, 'disposition', NEW.disposition, 'target_weight', NEW.target_weight, 'current_weight', NEW.current_weight, 'marginal_book_utility', NEW.marginal_book_utility, 'trace', NEW.trace, 'blockers', NEW.blockers, 'funding_source', NEW.funding_source, 'funding_amount', NEW.funding_amount, 'funding_sources', NEW.funding_sources)); RETURN NEW; END; $_$;

CREATE FUNCTION analysis.enforce_phase4_attribution_multiplier_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE expected_realized_pnl DOUBLE PRECISION; expected_allocation TEXT; expected_forecast TEXT;
                expected_hypothesis UUID; expected_action TEXT; expected_rank TEXT; expected_expression JSONB;
                expected_experiment TEXT; expected_trial UUID; expected_result UUID; expected_cutoff TIMESTAMPTZ;
        BEGIN
            NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
                'book_attribution_id', NEW.book_attribution_id, 'allocation_id', NEW.allocation_id,
                'allocation_item_id', NEW.allocation_item_id, 'strategy_forecast_id', NEW.strategy_forecast_id,
                'hypothesis_id', NEW.hypothesis_id, 'action_id', NEW.action_id, 'rank_id', NEW.rank_id,
                'expression', NEW.expression, 'experiment_id', NEW.experiment_id, 'trial_id', NEW.trial_id,
                'result_id', NEW.result_id, 'paper_execution_observation_id', NEW.paper_execution_observation_id,
                'pnl_status', NEW.pnl_status, 'realized_pnl', NEW.realized_pnl, 'attribution', NEW.attribution,
                'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff)
            ));
            IF NEW.pnl_status = 'realized' THEN
                SELECT sum((CASE WHEN observation.side = 'buy' THEN 1 ELSE -1 END)
                           * (observation.exit_price - observation.fill_price) * observation.filled_quantity
                           * observation.contract_multiplier - observation.event_fee)
                  INTO expected_realized_pnl
                  FROM app.paper_execution_observation observation
                 WHERE observation.allocation_item_id = NEW.allocation_item_id
                   AND observation.paper_order_id = (SELECT paper_order_id FROM app.paper_execution_observation WHERE paper_execution_observation_id = NEW.paper_execution_observation_id)
                   AND observation.status IN ('partial_exited', 'exited');
                IF expected_realized_pnl IS NULL OR abs(NEW.realized_pnl - expected_realized_pnl) > 0.000000001 THEN
                    RAISE EXCEPTION 'Phase 4 attribution does not match persisted event multiplier and fees';
                END IF;
            END IF;
            SELECT item.allocation_id, item.strategy_forecast_id, item.hypothesis_id, item.action_id, item.rank_id,
                   item.trace->'expression', forecast.research_trial_id, forecast.trial_result_id
              INTO expected_allocation, expected_forecast, expected_hypothesis, expected_action, expected_rank,
                   expected_expression, expected_trial, expected_result
              FROM analysis.portfolio_allocation_item item JOIN analysis.strategy_forecast forecast ON forecast.id = item.strategy_forecast_id
             WHERE item.allocation_item_id = NEW.allocation_item_id;
            IF expected_allocation IS NULL OR NEW.allocation_id IS DISTINCT FROM expected_allocation
               OR NEW.strategy_forecast_id IS DISTINCT FROM expected_forecast OR NEW.hypothesis_id IS DISTINCT FROM expected_hypothesis
               OR NEW.action_id IS DISTINCT FROM expected_action OR NEW.rank_id IS DISTINCT FROM expected_rank
               OR NEW.expression IS DISTINCT FROM expected_expression OR NEW.trial_id IS DISTINCT FROM expected_trial
               OR NEW.result_id IS DISTINCT FROM expected_result THEN RAISE EXCEPTION 'Phase 4 attribution does not match allocation lineage'; END IF;
            IF NEW.pnl_status = 'realized' AND NOT EXISTS (
                SELECT 1 FROM app.paper_execution_observation observation JOIN app.paper_order paper ON paper.id = observation.paper_order_id
                 WHERE observation.paper_execution_observation_id = NEW.paper_execution_observation_id
                   AND observation.allocation_item_id = NEW.allocation_item_id AND observation.paper_only
                   AND observation.execution_mode = 'paper' AND observation.status = 'exited' AND paper.status IN ('exited', 'closed')
            ) THEN RAISE EXCEPTION 'Phase 4 realized attribution requires a genuine linked paper fill'; END IF;
            SELECT input_cutoff INTO expected_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id;
            IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN RAISE EXCEPTION 'Phase 4 attribution input cutoff does not match allocation lineage'; END IF;
            SELECT decision.experiment_id INTO expected_experiment FROM analysis.ticker_decision decision
             WHERE decision.input_manifest->'trade_plan'->>'trade_plan_id' = NEW.action_id
               AND decision.input_manifest->'trade_plan'->>'rank_id' = NEW.rank_id
               AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id
               AND decision.status = 'published' AND decision.published_at IS NOT NULL
             ORDER BY decision.published_at DESC, decision.id DESC LIMIT 1;
            IF expected_experiment IS NULL OR NEW.experiment_id IS DISTINCT FROM expected_experiment THEN RAISE EXCEPTION 'Phase 4 attribution experiment lineage is invalid'; END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_phase4_authority_lineage() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ DECLARE allocation_meta JSONB; allocation_cutoff TIMESTAMPTZ; BEGIN SELECT metadata, input_cutoff INTO allocation_meta, allocation_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id; IF NEW.disposition = 'selected' AND NEW.ticker <> 'CASH' THEN IF allocation_meta IS NULL OR allocation_meta->>'authority' <> 'postgresql' OR NOT analysis.phase4_account_authority_exists( allocation_meta->>'authority_snapshot_id', allocation_cutoff ) OR NOT EXISTS ( SELECT 1 FROM analysis.ticker_decision decision JOIN analysis.strategy_forecast forecast ON forecast.id = NEW.strategy_forecast_id WHERE decision.id::text = NEW.trace->>'source_decision_id' AND decision.input_hash = NEW.trace->>'source_decision_input_hash' AND decision.status = 'published' AND decision.published_at IS NOT NULL AND decision.input_manifest->'trade_plan'->>'trade_plan_id' = NEW.action_id AND decision.input_manifest->'trade_plan'->>'rank_id' = NEW.rank_id AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id AND forecast.input_cutoff <= allocation_cutoff ) OR jsonb_typeof(allocation_meta->'source_hashes') <> 'array' OR NOT EXISTS ( SELECT 1 FROM jsonb_array_elements_text(allocation_meta->'source_hashes') hash WHERE hash = NEW.trace->>'source_decision_input_hash' ) THEN RAISE EXCEPTION 'Phase 4 allocation authority is not bound to PostgreSQL action lineage'; END IF; END IF; IF TG_OP = 'INSERT' THEN IF EXISTS ( SELECT 1 FROM analysis.portfolio_allocation_item item WHERE item.allocation_id = NEW.allocation_id AND item.disposition = 'selected' AND item.ticker <> 'CASH' AND NOT EXISTS ( SELECT 1 FROM jsonb_array_elements_text(allocation_meta->'source_hashes') hash WHERE hash = item.trace->>'source_decision_input_hash' ) ) THEN RAISE EXCEPTION 'Phase 4 allocation source hashes are incomplete'; END IF; END IF; RETURN NULL; END; $$;

CREATE FUNCTION analysis.enforce_phase4_authority_snapshot_complete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.status <> 'cash_only' AND NOT EXISTS (
                SELECT 1 FROM analysis.portfolio_allocation_item item
                 WHERE item.allocation_id = NEW.allocation_id
                   AND item.disposition = 'selected' AND item.ticker <> 'CASH'
            ) THEN RAISE EXCEPTION 'Phase 4 available allocation requires PostgreSQL-bound actions'; END IF;
            RETURN NULL;
        END;
        $$;

CREATE FUNCTION analysis.enforce_phase4_execution_snapshot_evidence() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE expected_cutoff TIMESTAMPTZ;
        BEGIN
            SELECT input_cutoff INTO expected_cutoff
            FROM analysis.portfolio_allocation_snapshot
            WHERE allocation_id = NEW.allocation_id;
            IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN
                RAISE EXCEPTION 'Phase 4 calibrated snapshot cutoff is not bound to its allocation';
            END IF;
            IF NEW.calibration_status = 'calibrated' AND (
                NEW.sample_count <= 0
                OR jsonb_typeof(NEW.metadata->'paper_observation_ids') IS DISTINCT FROM 'array'
                OR jsonb_array_length(NEW.metadata->'paper_observation_ids') <> NEW.sample_count
                OR EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'paper_observation_ids') id
                    JOIN app.paper_execution_observation observation ON observation.paper_execution_observation_id = id
                    JOIN analysis.portfolio_allocation_item item ON item.allocation_item_id = observation.allocation_item_id
                    JOIN app.paper_order paper ON paper.id = observation.paper_order_id
                    WHERE item.allocation_id <> NEW.allocation_id
                       OR observation.available_at > NEW.input_cutoff
                       OR observation.filled_quantity <= 0
                       OR observation.fill_price IS NULL
                       OR observation.observed_at IS DISTINCT FROM paper.filled_at
                       OR observation.available_at IS DISTINCT FROM paper.fill_evidence_at
                       OR paper.execution_quote IS NULL OR paper.fees IS NULL
                       OR paper.entry_slippage IS NULL OR paper.contract_multiplier IS NULL
                       OR paper.status NOT IN ('open', 'entered', 'partial_exited', 'exited', 'closed', 'invalidated')
                )
            ) THEN RAISE EXCEPTION 'Phase 4 calibrated snapshot requires matching persisted fill evidence'; END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_phase4_execution_snapshot_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE allocation_cutoff TIMESTAMPTZ;
        BEGIN
          SELECT input_cutoff INTO allocation_cutoff
            FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id;
          IF allocation_cutoff IS NULL OR NEW.input_cutoff <= allocation_cutoff THEN
            RAISE EXCEPTION 'Phase 4 execution snapshot cutoff must follow its allocation';
          END IF;
          IF NEW.calibration_status = 'calibrated' AND (
            NEW.sample_count <= 0 OR jsonb_typeof(NEW.metadata->'paper_observation_ids') IS DISTINCT FROM 'array'
            OR jsonb_array_length(NEW.metadata->'paper_observation_ids') <> NEW.sample_count
            OR EXISTS (
              SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'paper_observation_ids') id
              LEFT JOIN app.paper_execution_observation observation ON observation.paper_execution_observation_id = id
              LEFT JOIN analysis.portfolio_allocation_item item ON item.allocation_item_id = observation.allocation_item_id
              LEFT JOIN app.paper_order paper ON paper.id = observation.paper_order_id
              WHERE observation.paper_execution_observation_id IS NULL OR item.allocation_id <> NEW.allocation_id
                OR observation.available_at <= allocation_cutoff OR observation.available_at > NEW.input_cutoff
                OR observation.filled_quantity <= 0 OR observation.fill_price IS NULL
                OR observation.contract_multiplier IS NULL OR observation.event_fee IS NULL
                OR paper.submitted_at IS NULL OR paper.filled_at IS NULL OR paper.fill_evidence_at IS NULL
                OR paper.execution_quote IS NULL OR paper.actual_fill_price IS NULL)) THEN
            RAISE EXCEPTION 'Phase 4 calibrated snapshot requires matching post-allocation paper fills';
          END IF;
          RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_phase4_funding_conservation() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$
        DECLARE
          allocation_cutoff TIMESTAMPTZ;
          authority_id TEXT;
          account_nav DOUBLE PRECISION;
          source_key TEXT;
          claimed DOUBLE PRECISION;
          available DOUBLE PRECISION;
          source_capacity DOUBLE PRECISION;
        BEGIN
          SELECT snapshot.input_cutoff, snapshot.metadata->>'authority_snapshot_id'
            INTO allocation_cutoff, authority_id
            FROM analysis.portfolio_allocation_snapshot snapshot
           WHERE snapshot.allocation_id = NEW.allocation_id;
          IF allocation_cutoff IS NULL OR NOT analysis.phase4_account_authority_exists(authority_id, allocation_cutoff) THEN
            RAISE EXCEPTION 'Phase 4 funding conservation has no reconciled account authority';
          END IF;
          IF authority_id ~ '^broker-account:[0-9]+$' THEN
            SELECT net_liquidation::DOUBLE PRECISION INTO account_nav
              FROM raw.broker_account_snapshot
             WHERE ('broker-account:' || id::TEXT) = authority_id AND observed_at <= allocation_cutoff;
          ELSE
            SELECT net_liquidation::DOUBLE PRECISION INTO account_nav
              FROM app.manual_account_snapshot
             WHERE ('manual-account:' || id::TEXT) = authority_id
               AND reconciliation_state = 'reconciled' AND effective_at <= allocation_cutoff;
          END IF;
          IF account_nav IS NULL OR account_nav <= 0 THEN
            RAISE EXCEPTION 'Phase 4 funding conservation has incomplete account evidence';
          END IF;
          FOR source_key, claimed IN
            SELECT source.key, sum((source.value #>> '{}')::DOUBLE PRECISION)
              FROM analysis.portfolio_allocation_item item
              CROSS JOIN LATERAL jsonb_each(item.funding_sources) source
             WHERE item.allocation_id = NEW.allocation_id
               AND item.disposition = 'selected' AND item.ticker <> 'CASH'
               AND item.target_weight > item.current_weight
             GROUP BY source.key
          LOOP
            source_capacity := analysis.phase4_funding_source_capacity(source_key, authority_id, allocation_cutoff);
            IF source_capacity IS NULL OR claimed > source_capacity + 0.000000001 THEN
              RAISE EXCEPTION 'Phase 4 funding source is unavailable or over-allocated: %', source_key;
            END IF;
            IF source_key LIKE 'TRIM:%' THEN
              SELECT coalesce(sum(
                       least(greatest(item.current_weight - item.target_weight, 0) * account_nav,
                             source_capacity)
                     ), 0)
                INTO available
                FROM analysis.portfolio_allocation_item item
               WHERE item.allocation_id = NEW.allocation_id
                 AND item.disposition IN ('selected', 'rollback')
                 AND item.target_weight < item.current_weight
                 AND item.trace->>'trim_position_id' = replace(source_key, 'TRIM:', '');
              IF claimed > least(source_capacity, available) + 0.000000001 THEN
                RAISE EXCEPTION 'Phase 4 trim funding exceeds released source %', source_key;
              END IF;
            END IF;
          END LOOP;
          RETURN NULL;
        END;
        $_$;

CREATE FUNCTION analysis.enforce_phase4_funding_content() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
            'allocation_item_id', NEW.allocation_item_id, 'allocation_id', NEW.allocation_id,
            'candidate_id', NEW.candidate_id, 'ticker', NEW.ticker,
            'strategy_forecast_id', NEW.strategy_forecast_id, 'action_id', NEW.action_id,
            'rank_id', NEW.rank_id, 'hypothesis_id', NEW.hypothesis_id,
            'disposition', NEW.disposition, 'target_weight', NEW.target_weight,
            'current_weight', NEW.current_weight, 'marginal_book_utility', NEW.marginal_book_utility,
            'trace', NEW.trace, 'blockers', NEW.blockers, 'funding_source', NEW.funding_source,
            'funding_amount', NEW.funding_amount, 'funding_sources', NEW.funding_sources));
          RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_phase4_lineage() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ DECLARE expected_cutoff TIMESTAMPTZ; expected_allocation TEXT; expected_forecast TEXT; expected_hypothesis UUID; expected_action TEXT; expected_rank TEXT; expected_expression JSONB; expected_experiment TEXT; expected_trial UUID; expected_result UUID; scenario JSONB; probability_total DOUBLE PRECISION := 0; expected_realized_pnl DOUBLE PRECISION; BEGIN IF TG_TABLE_NAME = 'portfolio_allocation_snapshot' THEN NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'allocation_id', NEW.allocation_id, 'as_of', analysis.phase4_canonical_timestamp(NEW.as_of), 'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff), 'status', NEW.status, 'cash_hurdle', NEW.cash_hurdle, 'forecast_ids', NEW.forecast_ids, 'action_ids', NEW.action_ids, 'strategy_registry_ids', NEW.strategy_registry_ids, 'metadata', NEW.metadata )); ELSIF TG_TABLE_NAME = 'portfolio_allocation_item' THEN NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'allocation_item_id', NEW.allocation_item_id, 'allocation_id', NEW.allocation_id, 'candidate_id', NEW.candidate_id, 'ticker', NEW.ticker, 'strategy_forecast_id', NEW.strategy_forecast_id, 'action_id', NEW.action_id, 'rank_id', NEW.rank_id, 'hypothesis_id', NEW.hypothesis_id, 'disposition', NEW.disposition, 'target_weight', NEW.target_weight, 'current_weight', NEW.current_weight, 'marginal_book_utility', NEW.marginal_book_utility, 'trace', NEW.trace, 'blockers', NEW.blockers, 'funding_source', NEW.funding_source, 'funding_amount', NEW.funding_amount )); ELSIF TG_TABLE_NAME = 'probabilistic_portfolio_scenario_artifact' THEN NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'scenario_artifact_id', NEW.scenario_artifact_id, 'allocation_id', NEW.allocation_id, 'model_version', NEW.model_version, 'probability_semantics', NEW.probability_semantics, 'scenarios', NEW.scenarios, 'tail_dependence', NEW.tail_dependence, 'simultaneous_unwind', NEW.simultaneous_unwind, 'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff) )); ELSIF TG_TABLE_NAME = 'execution_model_snapshot' THEN NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'execution_model_snapshot_id', NEW.execution_model_snapshot_id, 'allocation_id', NEW.allocation_id, 'model_version', NEW.model_version, 'calibration_status', NEW.calibration_status, 'sample_count', NEW.sample_count, 'fill_probability', NEW.fill_probability, 'spread_bps', NEW.spread_bps, 'latency_ms', NEW.latency_ms, 'impact_bps', NEW.impact_bps, 'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff), 'metadata', NEW.metadata )); ELSIF TG_TABLE_NAME = 'book_attribution' THEN NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'book_attribution_id', NEW.book_attribution_id, 'allocation_id', NEW.allocation_id, 'allocation_item_id', NEW.allocation_item_id, 'strategy_forecast_id', NEW.strategy_forecast_id, 'hypothesis_id', NEW.hypothesis_id, 'action_id', NEW.action_id, 'rank_id', NEW.rank_id, 'expression', NEW.expression, 'experiment_id', NEW.experiment_id, 'trial_id', NEW.trial_id, 'result_id', NEW.result_id, 'paper_execution_observation_id', NEW.paper_execution_observation_id, 'pnl_status', NEW.pnl_status, 'realized_pnl', NEW.realized_pnl, 'attribution', NEW.attribution, 'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff) )); ELSIF TG_TABLE_NAME = 'portfolio_drift_evidence' THEN NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'decision_id', NEW.decision_id, 'allocation_id', NEW.allocation_id, 'allocation_item_id', NEW.allocation_item_id, 'drift_score', NEW.drift_score, 'rollback_threshold', NEW.rollback_threshold, 'proposed_weight', NEW.proposed_weight, 'action', NEW.action, 'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff), 'metadata', NEW.metadata )); END IF; IF TG_TABLE_NAME = 'portfolio_allocation_item' THEN IF NEW.disposition = 'selected' AND NEW.ticker <> 'CASH' THEN IF NEW.funding_amount IS NULL OR NEW.funding_amount <= 0 THEN RAISE EXCEPTION 'Phase 4 funded item requires a positive funding amount'; ELSIF coalesce((SELECT metadata->>'execution_status' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id), '') <> 'calibrated' THEN RAISE EXCEPTION 'Phase 4 funded item requires a fresh calibrated execution snapshot'; ELSIF NEW.funding_source LIKE ANY (ARRAY['CASH:broker-account:%', 'CASH:manual-account:%']) AND (analysis.phase4_funding_source_capacity( NEW.funding_source, (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id), (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) ) IS NULL OR analysis.phase4_funding_source_capacity( NEW.funding_source, (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id), (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) ) < NEW.funding_amount) THEN RAISE EXCEPTION 'Phase 4 cash funding source is not an actual PostgreSQL account snapshot'; ELSIF NEW.funding_source LIKE ANY (ARRAY['TRIM:broker-position:%', 'TRIM:manual-position:%']) AND (analysis.phase4_funding_source_capacity( NEW.funding_source, (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id), (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) ) IS NULL OR analysis.phase4_funding_source_capacity( NEW.funding_source, (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id), (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) ) < NEW.funding_amount) THEN RAISE EXCEPTION 'Phase 4 trim funding source is not an actual PostgreSQL position'; ELSIF NOT (NEW.funding_source LIKE ANY (ARRAY[ 'CASH:broker-account:%', 'CASH:manual-account:%', 'TRIM:broker-position:%', 'TRIM:manual-position:%'])) THEN RAISE EXCEPTION 'Phase 4 funded item has no authoritative funding source'; END IF; END IF; IF NEW.strategy_forecast_id IS NOT NULL THEN SELECT forecast.input_cutoff, forecast.id, revision.hypothesis_id INTO expected_cutoff, expected_forecast, expected_hypothesis FROM analysis.strategy_forecast forecast JOIN analysis.strategy_revision revision ON revision.id = forecast.strategy_revision_id WHERE forecast.id = NEW.strategy_forecast_id; IF expected_forecast IS NULL OR expected_cutoff > (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) OR NEW.hypothesis_id IS DISTINCT FROM expected_hypothesis THEN RAISE EXCEPTION 'Phase 4 allocation forecast or PIT lineage is invalid'; END IF; IF NEW.action_id IS NOT NULL AND NOT EXISTS ( SELECT 1 FROM analysis.ticker_decision decision WHERE decision.input_manifest->'trade_plan'->>'trade_plan_id' = NEW.action_id AND decision.input_manifest->'trade_plan'->>'rank_id' = NEW.rank_id AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id AND decision.status = 'published' AND decision.published_at IS NOT NULL AND decision.as_of <= (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) AND decision.published_at <= (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) ) THEN RAISE EXCEPTION 'Phase 4 allocation action or rank lineage is invalid'; END IF; END IF; ELSIF TG_TABLE_NAME = 'probabilistic_portfolio_scenario_artifact' THEN IF jsonb_array_length(NEW.scenarios) = 0 OR jsonb_array_length(NEW.scenarios) > 64 THEN RAISE EXCEPTION 'Phase 4 scenario paths must be bounded and non-empty'; END IF; FOR scenario IN SELECT value FROM jsonb_array_elements(NEW.scenarios) LOOP IF jsonb_typeof(scenario->'probability') IS DISTINCT FROM 'number' OR jsonb_typeof(scenario->'returns') IS DISTINCT FROM 'object' OR scenario->'returns' = '{}'::jsonb OR jsonb_typeof(scenario->'shocks') IS DISTINCT FROM 'object' OR scenario->'shocks' = '{}'::jsonb OR scenario->'returns' = scenario->'shocks' THEN RAISE EXCEPTION 'Phase 4 scenario path requires probability, returns, and shocks'; END IF; IF jsonb_typeof(scenario->'provenance') IS DISTINCT FROM 'array' OR jsonb_array_length(scenario->'provenance') = 0 OR EXISTS ( SELECT 1 FROM jsonb_array_elements(scenario->'provenance') source WHERE jsonb_typeof(source->'strategy_pnl_tape_id') IS DISTINCT FROM 'string' OR jsonb_typeof(source->'pnl_date') IS DISTINCT FROM 'string' OR jsonb_typeof(source->'input_cutoff') IS DISTINCT FROM 'string' OR jsonb_typeof(source->'available_at') IS DISTINCT FROM 'string' OR jsonb_typeof(source->'input_hash') IS DISTINCT FROM 'string' ) THEN RAISE EXCEPTION 'Phase 4 scenario path requires complete persisted tape provenance'; END IF; probability_total := probability_total + (scenario->>'probability')::DOUBLE PRECISION; END LOOP; IF EXISTS ( SELECT 1 FROM jsonb_array_elements(NEW.scenarios) path CROSS JOIN LATERAL jsonb_array_elements(path->'provenance') source WHERE NOT EXISTS ( SELECT 1 FROM analysis.strategy_pnl_tape tape JOIN analysis.portfolio_allocation_item item ON item.allocation_id = NEW.allocation_id AND item.strategy_forecast_id = tape.strategy_forecast_id AND item.disposition = 'selected' WHERE tape.id::text = source->>'strategy_pnl_tape_id' AND tape.input_hash::text = source->>'input_hash' AND tape.input_cutoff <= NEW.input_cutoff AND tape.available_at <= NEW.input_cutoff ) ) THEN RAISE EXCEPTION 'Phase 4 scenario provenance is not bound to a persisted tape row'; END IF; IF probability_total < 0.999999 OR probability_total > 1.000001 THEN RAISE EXCEPTION 'Phase 4 scenario probabilities must sum to one'; END IF; IF NOT (NEW.tail_dependence ? 'negative_return_co_exceedance' OR NEW.tail_dependence ? 'co_exceedance') THEN RAISE EXCEPTION 'Phase 4 scenario artifact requires persisted tail co-exceedance results'; END IF; IF NOT (NEW.simultaneous_unwind ? 'probability' AND NEW.simultaneous_unwind ? 'observations') THEN RAISE EXCEPTION 'Phase 4 scenario artifact requires simultaneous-unwind results'; END IF; SELECT input_cutoff INTO expected_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id; IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN RAISE EXCEPTION 'Phase 4 scenario input cutoff does not match allocation lineage'; END IF; ELSIF TG_TABLE_NAME = 'execution_model_snapshot' THEN IF NEW.allocation_id IS NULL THEN RAISE EXCEPTION 'Phase 4 execution snapshot requires allocation lineage'; END IF; SELECT input_cutoff INTO expected_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id; IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN RAISE EXCEPTION 'Phase 4 execution input cutoff does not match allocation lineage'; END IF; IF NEW.sample_count > 0 AND ( jsonb_typeof(NEW.metadata->'paper_observation_ids') IS DISTINCT FROM 'array' OR jsonb_array_length(NEW.metadata->'paper_observation_ids') <> NEW.sample_count OR EXISTS ( SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'paper_observation_ids') observation_id WHERE NOT EXISTS ( SELECT 1 FROM app.paper_execution_observation observation JOIN app.paper_order paper ON paper.id = observation.paper_order_id WHERE observation.paper_execution_observation_id = observation_id AND observation.allocation_item_id IN ( SELECT allocation_item_id FROM analysis.portfolio_allocation_item WHERE allocation_id = NEW.allocation_id ) AND observation.paper_only AND observation.execution_mode = 'paper' AND observation.filled_quantity > 0 AND observation.fill_price IS NOT NULL AND paper.status IN ('open', 'entered', 'partial_exited', 'exited', 'closed', 'invalidated') ) ) ) THEN RAISE EXCEPTION 'Phase 4 execution snapshot is not bound to genuine paper fills'; END IF; ELSIF TG_TABLE_NAME = 'book_attribution' THEN SELECT item.allocation_id, item.strategy_forecast_id, item.hypothesis_id, item.action_id, item.rank_id, item.trace->'expression', forecast.research_trial_id, forecast.trial_result_id INTO expected_allocation, expected_forecast, expected_hypothesis, expected_action, expected_rank, expected_expression, expected_trial, expected_result FROM analysis.portfolio_allocation_item item JOIN analysis.strategy_forecast forecast ON forecast.id = item.strategy_forecast_id WHERE item.allocation_item_id = NEW.allocation_item_id; IF expected_allocation IS NULL OR NEW.allocation_id IS DISTINCT FROM expected_allocation THEN RAISE EXCEPTION 'Phase 4 attribution item does not match allocation lineage'; END IF; IF NEW.pnl_status = 'realized' AND (NEW.paper_execution_observation_id IS NULL OR NOT EXISTS ( SELECT 1 FROM app.paper_execution_observation observation JOIN app.paper_order paper ON paper.id = observation.paper_order_id WHERE observation.paper_execution_observation_id = NEW.paper_execution_observation_id AND observation.allocation_item_id = NEW.allocation_item_id AND observation.paper_only AND observation.execution_mode = 'paper' AND observation.filled_quantity > 0 AND observation.fill_price IS NOT NULL AND observation.exit_price IS NOT NULL AND paper.status IN ('exited', 'closed') )) THEN RAISE EXCEPTION 'Phase 4 realized attribution requires a genuine linked paper fill'; END IF; IF NEW.strategy_forecast_id IS DISTINCT FROM expected_forecast OR NEW.hypothesis_id IS DISTINCT FROM expected_hypothesis OR NEW.action_id IS DISTINCT FROM expected_action OR NEW.rank_id IS DISTINCT FROM expected_rank OR NEW.expression IS DISTINCT FROM expected_expression OR NEW.trial_id IS DISTINCT FROM expected_trial OR NEW.result_id IS DISTINCT FROM expected_result THEN RAISE EXCEPTION 'Phase 4 attribution does not match allocation lineage'; END IF; IF NOT EXISTS ( SELECT 1 FROM analysis.ticker_decision decision WHERE decision.input_manifest->'trade_plan'->>'trade_plan_id' = ( SELECT action_id FROM analysis.portfolio_allocation_item WHERE allocation_item_id = NEW.allocation_item_id) AND decision.input_manifest->'trade_plan'->>'rank_id' = ( SELECT rank_id FROM analysis.portfolio_allocation_item WHERE allocation_item_id = NEW.allocation_item_id) AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id AND decision.status = 'published' AND decision.published_at IS NOT NULL ) THEN RAISE EXCEPTION 'Phase 4 attribution requires a published action and rank lineage'; END IF; SELECT decision.experiment_id INTO expected_experiment FROM analysis.ticker_decision decision WHERE decision.input_manifest->'trade_plan'->>'trade_plan_id' = NEW.action_id AND decision.input_manifest->'trade_plan'->>'rank_id' = NEW.rank_id AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id AND decision.status = 'published' AND decision.published_at IS NOT NULL ORDER BY decision.published_at DESC, decision.id DESC LIMIT 1; IF expected_experiment IS NULL OR NEW.experiment_id IS DISTINCT FROM expected_experiment THEN RAISE EXCEPTION 'Phase 4 attribution experiment lineage is invalid'; END IF; SELECT input_cutoff INTO expected_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id; IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN RAISE EXCEPTION 'Phase 4 attribution input cutoff does not match allocation lineage'; END IF; IF NEW.pnl_status = 'realized' THEN SELECT CASE WHEN observation.side = 'buy' THEN 1 ELSE -1 END * (observation.exit_price - observation.fill_price) * observation.filled_quantity - coalesce(paper.fees, 0) INTO expected_realized_pnl FROM app.paper_execution_observation observation JOIN app.paper_order paper ON paper.id = observation.paper_order_id WHERE observation.paper_execution_observation_id = NEW.paper_execution_observation_id; IF expected_realized_pnl IS NULL OR abs(NEW.realized_pnl - expected_realized_pnl) > 0.000000001 THEN RAISE EXCEPTION 'Phase 4 attribution P&L does not match the persisted fill and fee lineage'; END IF; END IF; END IF; RETURN NEW; END; $$;

CREATE FUNCTION analysis.enforce_phase4_paper_execution() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE order_created_at TIMESTAMPTZ; order_status TEXT;
                order_filled_quantity DOUBLE PRECISION; order_fill_price DOUBLE PRECISION;
                order_filled_at TIMESTAMPTZ; order_exit_at TIMESTAMPTZ;
        BEGIN
            SELECT created_at, status, coalesce(filled_quantity, 0)::DOUBLE PRECISION,
                   actual_fill_price::DOUBLE PRECISION, filled_at, exit_at
              INTO order_created_at, order_status, order_filled_quantity,
                   order_fill_price, order_filled_at, order_exit_at
            FROM app.paper_order WHERE id = NEW.paper_order_id;
            IF order_created_at IS NULL
               OR NEW.observed_at < order_created_at
               OR NEW.available_at < NEW.observed_at THEN
                RAISE EXCEPTION 'Phase 4 paper observation has invalid order clock lineage';
            END IF;
            IF NEW.filled_quantity > 0
               AND (order_status NOT IN ('open', 'entered', 'partial_exited', 'exited', 'invalidated', 'closed')
                    OR order_filled_quantity < NEW.filled_quantity
                    OR order_fill_price IS NULL OR order_filled_at IS NULL
                    OR NEW.observed_at < order_filled_at) THEN
                RAISE EXCEPTION 'Phase 4 paper observation requires a genuine paper fill';
            END IF;
            IF NEW.status = 'exited' AND (order_exit_at IS NULL OR NEW.observed_at < order_exit_at) THEN
                RAISE EXCEPTION 'Phase 4 exit observation requires an exited paper order';
            END IF;
            IF NEW.allocation_item_id IS NULL OR NEW.action_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM analysis.portfolio_allocation_item item
                WHERE item.allocation_item_id = NEW.allocation_item_id
                  AND item.action_id = NEW.action_id
                  AND EXISTS (
                      SELECT 1 FROM app.paper_order paper
                      WHERE paper.id = NEW.paper_order_id
                        AND paper.policy_result->>'trade_plan_id' = NEW.action_id
                  )
            ) THEN
                RAISE EXCEPTION 'Phase 4 paper observation action lineage is invalid';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_phase4_paper_execution_evidence() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE paper app.paper_order%ROWTYPE;
        BEGIN
            SELECT * INTO paper FROM app.paper_order WHERE id = NEW.paper_order_id;
            IF NEW.filled_quantity > 0 AND (paper.id IS NULL OR NOT paper.paper_only OR paper.submitted_at IS NULL
               OR paper.filled_at IS NULL OR paper.fill_evidence_at IS NULL
               OR paper.execution_quote IS NULL OR paper.fees IS NULL OR paper.entry_slippage IS NULL
               OR paper.contract_multiplier IS NULL OR paper.filled_quantity <= 0
               OR paper.actual_fill_price IS NULL OR paper.fill_evidence_at <= paper.filled_at
               OR NEW.observed_at IS DISTINCT FROM paper.filled_at
               OR NEW.available_at IS DISTINCT FROM paper.fill_evidence_at
               OR NEW.fill_price IS DISTINCT FROM paper.actual_fill_price
               OR NEW.filled_quantity > paper.filled_quantity) THEN
                RAISE EXCEPTION 'Phase 4 observation requires persisted paper fill evidence';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_phase4_paper_execution_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE paper app.paper_order%ROWTYPE;
        BEGIN
            SELECT * INTO paper FROM app.paper_order WHERE id = NEW.paper_order_id;
            IF NEW.filled_quantity > 0 AND (paper.id IS NULL OR NOT paper.paper_only OR paper.submitted_at IS NULL
               OR paper.filled_at IS NULL OR paper.fill_evidence_at IS NULL OR paper.execution_quote IS NULL
               OR paper.entry_slippage IS NULL OR paper.contract_multiplier IS NULL OR paper.filled_quantity <= 0
               OR paper.actual_fill_price IS NULL OR NEW.contract_multiplier IS NULL OR NEW.event_fee IS NULL
               OR NEW.contract_multiplier <> paper.contract_multiplier OR NEW.event_fee < 0
               OR NEW.filled_quantity > paper.filled_quantity OR NEW.fill_price IS DISTINCT FROM paper.actual_fill_price
               OR (NEW.status NOT IN ('exited', 'partial_exited') AND (NEW.observed_at IS DISTINCT FROM paper.filled_at OR NEW.available_at IS DISTINCT FROM paper.fill_evidence_at))
               OR (NEW.status IN ('exited', 'partial_exited') AND (paper.exit_at IS NULL OR NEW.observed_at IS DISTINCT FROM paper.exit_at OR NEW.available_at < NEW.observed_at))) THEN
                RAISE EXCEPTION 'Phase 4 observation requires persisted paper fill evidence';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_phase4_review_item_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$ DECLARE expected_cutoff TIMESTAMPTZ; expected_forecast TEXT; expected_hypothesis UUID; source_key TEXT; source_value JSONB; source_amount DOUBLE PRECISION; source_total DOUBLE PRECISION := 0; source_count INTEGER := 0; authority_account BIGINT; BEGIN SELECT input_cutoff INTO expected_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id; IF expected_cutoff IS NULL THEN RAISE EXCEPTION 'Phase 4 allocation item has no persisted allocation cutoff'; END IF; IF NEW.funding_sources IS NULL OR jsonb_typeof(NEW.funding_sources) IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'Phase 4 funding sources must be an object'; END IF; FOR source_key, source_value IN SELECT key, value FROM jsonb_each(NEW.funding_sources) LOOP source_count := source_count + 1; IF source_key !~ '^(CASH:(broker-account|manual-account):[0-9]+|TRIM:(broker-position|manual-position):[0-9]+)$' OR jsonb_typeof(source_value) IS DISTINCT FROM 'number' THEN RAISE EXCEPTION 'Phase 4 funding source is not a canonical PostgreSQL source: %', source_key; END IF; source_amount := (source_value #>> '{}')::DOUBLE PRECISION; IF source_amount IS NULL OR source_amount <= 0 OR source_amount = 'Infinity'::DOUBLE PRECISION OR source_amount = '-Infinity'::DOUBLE PRECISION THEN RAISE EXCEPTION 'Phase 4 funding source amount is invalid: %', source_key; END IF; source_total := source_total + source_amount; IF (SELECT count(*) FROM jsonb_object_keys(NEW.funding_sources)) = 1 AND NEW.funding_amount IS NOT NULL AND abs(source_amount - NEW.funding_amount) > 0.000000001 THEN RAISE EXCEPTION 'Phase 4 funding sources do not conserve funding_amount'; END IF; IF analysis.phase4_funding_source_capacity( source_key, (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id), expected_cutoff ) IS NULL OR analysis.phase4_funding_source_capacity( source_key, (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id), expected_cutoff ) < source_amount THEN RAISE EXCEPTION 'Phase 4 funding source is unavailable or over-allocated: %', source_key; END IF; END LOOP; IF source_count > 0 AND (NEW.funding_amount IS NULL OR abs(source_total - NEW.funding_amount) > 0.000000001) THEN RAISE EXCEPTION 'Phase 4 funding sources do not conserve funding_amount'; END IF; IF NEW.funding_amount IS NOT NULL AND source_count = 0 AND NEW.disposition IN ('selected', 'rollback') AND NEW.ticker <> 'CASH' AND NEW.target_weight <> NEW.current_weight THEN RAISE EXCEPTION 'Phase 4 funded item must name every source'; END IF; IF source_count = 1 AND NEW.funding_source IS DISTINCT FROM ( SELECT min(source.key) FROM jsonb_object_keys(NEW.funding_sources) AS source(key) ) THEN RAISE EXCEPTION 'Phase 4 single-source display does not match its source map'; END IF; IF source_count > 1 AND NEW.funding_source IS DISTINCT FROM 'MULTI_SOURCE' THEN RAISE EXCEPTION 'Phase 4 multi-source funding must use display marker MULTI_SOURCE'; END IF; IF NEW.disposition = 'selected' AND NEW.ticker <> 'CASH' AND NEW.target_weight > NEW.current_weight AND (NEW.funding_amount IS NULL OR NEW.funding_amount <= 0 OR source_count = 0) THEN RAISE EXCEPTION 'Phase 4 increase requires conserved PostgreSQL funding sources'; END IF; IF NEW.disposition IN ('selected', 'rollback') AND NEW.target_weight < NEW.current_weight THEN IF source_count = 0 OR source_count <> 1 OR NEW.funding_source IS NULL OR NEW.funding_source !~ '^TRIM:broker-position:[0-9]+$' OR NEW.trace->>'trim_position_id' IS DISTINCT FROM split_part(NEW.funding_source, ':', 2) || ':' || split_part(NEW.funding_source, ':', 3) THEN RAISE EXCEPTION 'Phase 4 trim release is not bound to one persisted position'; END IF; END IF; NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'allocation_item_id', NEW.allocation_item_id, 'allocation_id', NEW.allocation_id, 'candidate_id', NEW.candidate_id, 'ticker', NEW.ticker, 'strategy_forecast_id', NEW.strategy_forecast_id, 'action_id', NEW.action_id, 'rank_id', NEW.rank_id, 'hypothesis_id', NEW.hypothesis_id, 'disposition', NEW.disposition, 'target_weight', NEW.target_weight, 'current_weight', NEW.current_weight, 'marginal_book_utility', NEW.marginal_book_utility, 'trace', NEW.trace, 'blockers', NEW.blockers, 'funding_source', NEW.funding_source, 'funding_amount', NEW.funding_amount, 'funding_sources', NEW.funding_sources)); IF NEW.strategy_forecast_id IS NOT NULL THEN SELECT forecast.input_cutoff, forecast.id, revision.hypothesis_id INTO expected_cutoff, expected_forecast, expected_hypothesis FROM analysis.strategy_forecast forecast JOIN analysis.strategy_revision revision ON revision.id = forecast.strategy_revision_id WHERE forecast.id = NEW.strategy_forecast_id; IF expected_forecast IS NULL OR expected_cutoff > (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) OR NEW.hypothesis_id IS DISTINCT FROM expected_hypothesis THEN RAISE EXCEPTION 'Phase 4 allocation forecast lineage is invalid'; END IF; END IF; RETURN NEW; END; $_$;

CREATE FUNCTION analysis.enforce_phase4_review_snapshot_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$
        BEGIN
          IF coalesce(NEW.metadata->>'authority', '') <> 'postgresql'
             OR NEW.metadata->>'authority_snapshot_id' !~ '^(broker-account|manual-account):[0-9]+$'
             OR jsonb_typeof(NEW.metadata->'source_hashes') IS DISTINCT FROM 'array'
             OR (NEW.status <> 'cash_only' AND jsonb_array_length(NEW.metadata->'source_hashes') = 0)
             OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'source_hashes') hash
                        WHERE hash !~ '^[0-9a-f]{64}$' OR hash = repeat('0', 64))
             OR NOT analysis.phase4_account_authority_exists(
                  NEW.metadata->>'authority_snapshot_id', NEW.input_cutoff)
          THEN
            RAISE EXCEPTION 'Phase 4 allocation requires PostgreSQL authority evidence';
          END IF;
          NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
            'allocation_id', NEW.allocation_id, 'as_of', analysis.phase4_canonical_timestamp(NEW.as_of),
            'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff), 'status', NEW.status,
            'cash_hurdle', NEW.cash_hurdle, 'forecast_ids', NEW.forecast_ids, 'action_ids', NEW.action_ids,
            'strategy_registry_ids', NEW.strategy_registry_ids, 'metadata', NEW.metadata));
          RETURN NEW;
        END;
        $_$;

CREATE FUNCTION analysis.enforce_phase4_source_lineage() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
          allocation_meta JSONB;
          expected_hashes JSONB;
          expected_forecasts JSONB;
          expected_actions JSONB;
          stored_forecasts JSONB;
          stored_actions JSONB;
        BEGIN
          SELECT snapshot.metadata, snapshot.forecast_ids, snapshot.action_ids
            INTO allocation_meta, stored_forecasts, stored_actions
            FROM analysis.portfolio_allocation_snapshot snapshot
           WHERE snapshot.allocation_id = NEW.allocation_id;
          IF allocation_meta IS NULL THEN
            RAISE EXCEPTION 'Phase 4 source lineage has no persisted allocation authority';
          END IF;
          SELECT coalesce(jsonb_agg(DISTINCT item.trace->>'source_decision_input_hash'
                                    ORDER BY item.trace->>'source_decision_input_hash'), '[]'::jsonb),
                 coalesce(jsonb_agg(DISTINCT item.strategy_forecast_id ORDER BY item.strategy_forecast_id), '[]'::jsonb),
                 coalesce(jsonb_agg(DISTINCT item.action_id ORDER BY item.action_id), '[]'::jsonb)
            INTO expected_hashes, expected_forecasts, expected_actions
            FROM analysis.portfolio_allocation_item item
           WHERE item.allocation_id = NEW.allocation_id
             AND item.ticker <> 'CASH'
             AND item.disposition IN ('selected', 'rollback');
          IF expected_hashes <> '[]'::jsonb
             AND (allocation_meta->'source_hashes' IS DISTINCT FROM expected_hashes
                  OR stored_forecasts IS DISTINCT FROM expected_forecasts
                  OR stored_actions IS DISTINCT FROM expected_actions) THEN
            RAISE EXCEPTION 'Phase 4 source hashes or action identities are not canonical';
          END IF;
          IF expected_hashes = '[]'::jsonb
             AND (stored_forecasts <> '[]'::jsonb OR stored_actions <> '[]'::jsonb) THEN
            RAISE EXCEPTION 'Phase 4 empty allocation has non-empty action identities';
          END IF;
          RETURN NULL;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_authority_availability() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            NEW.created_at := actual;
            NEW.available_at := actual;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_evaluator_output() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis'
    AS $_$
        DECLARE
            result_trial UUID; result_hash TEXT; result_outcome JSONB; trial_cutoff TIMESTAMPTZ;
            run_input_hash TEXT; run_cutoff TIMESTAMPTZ; run_code TEXT;
            universe_digest TEXT; actual TIMESTAMPTZ := clock_timestamp();
            expected_hash TEXT; expected_signature TEXT; signing_key TEXT;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'research evaluator outputs are immutable';
            END IF;
            SELECT result.research_trial_id, result.input_hash, result.outcome, trial.input_cutoff
              INTO result_trial, result_hash, result_outcome, trial_cutoff
            FROM analysis.trial_result result
            JOIN analysis.research_trial trial ON trial.id = result.research_trial_id
            WHERE result.id = NEW.trial_result_id;
            SELECT input_hash, input_cutoff, code_version
              INTO run_input_hash, run_cutoff, run_code
            FROM analysis.run WHERE id = NEW.analysis_run_id;
            SELECT manifest.manifest_hash INTO universe_digest
            FROM analysis.trial_universe_manifest manifest
            WHERE manifest.research_trial_id = result_trial;
            signing_key := analysis.research_evaluator_signing_key();
            IF signing_key IS NULL OR length(signing_key) < 16 THEN
                RAISE EXCEPTION 'protected research evaluator signing key is not configured';
            END IF;
            IF result_trial IS NULL OR result_trial <> NEW.research_trial_id
               OR result_outcome->>'passed' IS DISTINCT FROM 'true'
               OR run_input_hash IS DISTINCT FROM result_hash
               OR run_cutoff IS DISTINCT FROM trial_cutoff
               OR run_code IS DISTINCT FROM NEW.evaluator_code_version
               OR NEW.input_hash IS DISTINCT FROM result_hash
               OR NEW.universe_hash IS DISTINCT FROM universe_digest
               OR NEW.raw_output->>'trial_input_hash' IS DISTINCT FROM result_hash
               OR NEW.raw_output->>'input_hash' IS DISTINCT FROM NEW.input_hash
               OR NEW.raw_output->>'universe_hash' IS DISTINCT FROM NEW.universe_hash
               OR NEW.raw_output->>'feature_hash' IS DISTINCT FROM NEW.feature_hash
               OR NEW.raw_output->>'evaluator_id' IS DISTINCT FROM NEW.evaluator_id
               OR NEW.raw_output->>'evaluator_code_version' IS DISTINCT FROM NEW.evaluator_code_version
               OR NEW.raw_output->>'evidence_kind' IS DISTINCT FROM NEW.evidence_kind
               OR NEW.evaluator_id IS NULL OR length(trim(NEW.evaluator_id)) < 3
               OR NEW.evaluator_id LIKE '%' || chr(31) || '%'
               OR NEW.evaluator_code_version IS NULL OR length(trim(NEW.evaluator_code_version)) < 3
               OR NEW.evaluator_code_version LIKE '%' || chr(31) || '%'
               OR NEW.signature !~ '^[0-9a-fA-F]{64}$'
               OR NEW.available_at > actual
               OR NOT NEW.domain_valid OR NEW.sample_count <= 0
            THEN
                RAISE EXCEPTION 'signed evaluator output is not linked to the protected authoritative trial and run';
            END IF;
            IF NEW.evidence_kind = 'controls' THEN
                IF jsonb_typeof(NEW.raw_output->'randomized_label_samples') <> 'array'
                   OR jsonb_typeof(NEW.raw_output->'white_noise_samples') <> 'array'
                   OR jsonb_array_length(NEW.raw_output->'randomized_label_samples') = 0
                   OR jsonb_array_length(NEW.raw_output->'white_noise_samples') = 0
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.raw_output->'randomized_label_samples') value WHERE jsonb_typeof(value) <> 'number')
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.raw_output->'white_noise_samples') value WHERE jsonb_typeof(value) <> 'number')
                   OR NEW.sample_count <> jsonb_array_length(NEW.raw_output->'randomized_label_samples') + jsonb_array_length(NEW.raw_output->'white_noise_samples')
                THEN RAISE EXCEPTION 'control evaluator output is incomplete'; END IF;
            ELSIF NEW.evidence_kind = 'cpcv_paths' THEN
                IF NOT analysis.research_combinatorial_paths_complete(NEW.raw_output)
                   OR NEW.sample_count <> (NEW.raw_output->>'path_count')::INTEGER
                THEN RAISE EXCEPTION 'CPCV evaluator output is incomplete'; END IF;
            ELSIF NEW.evidence_kind = 'neutralization' THEN
                IF jsonb_typeof(NEW.raw_output->'samples') <> 'array'
                   OR jsonb_array_length(NEW.raw_output->'samples') = 0
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.raw_output->'samples') value WHERE jsonb_typeof(value) <> 'number')
                   OR NEW.sample_count <> jsonb_array_length(NEW.raw_output->'samples')
                THEN RAISE EXCEPTION 'neutralization evaluator output is incomplete'; END IF;
            ELSIF NEW.evidence_kind = 'parameter_stability' THEN
                IF jsonb_typeof(NEW.raw_output->'samples') <> 'array'
                   OR jsonb_array_length(NEW.raw_output->'samples') < 3
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.raw_output->'samples') value WHERE jsonb_typeof(value) <> 'number')
                   OR NEW.sample_count <> jsonb_array_length(NEW.raw_output->'samples')
                THEN RAISE EXCEPTION 'stability evaluator output is incomplete'; END IF;
            ELSIF NEW.evidence_kind = 'mechanism_falsification' THEN
                IF jsonb_typeof(NEW.raw_output->'samples') <> 'array'
                   OR jsonb_array_length(NEW.raw_output->'samples') = 0
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.raw_output->'samples') value WHERE jsonb_typeof(value) <> 'number')
                   OR NEW.sample_count <> jsonb_array_length(NEW.raw_output->'samples')
                THEN RAISE EXCEPTION 'mechanism evaluator output is incomplete'; END IF;
            ELSIF NEW.evidence_kind = 'multiple_testing' THEN
                IF jsonb_typeof(NEW.raw_output->'path_returns') <> 'array'
                   OR jsonb_typeof(NEW.raw_output->'p_values') <> 'array'
                   OR jsonb_array_length(NEW.raw_output->'path_returns') = 0
                   OR jsonb_array_length(NEW.raw_output->'p_values') = 0
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.raw_output->'path_returns') value WHERE jsonb_typeof(value) <> 'number')
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.raw_output->'p_values') value WHERE jsonb_typeof(value) <> 'number' OR value::TEXT::DOUBLE PRECISION NOT BETWEEN 0 AND 1)
                   OR jsonb_typeof(NEW.raw_output->'metrics') <> 'object'
                   OR NEW.raw_output->'metrics'->>'domain_valid' <> 'true'
                   OR NOT (NEW.raw_output->'metrics' ?& ARRAY['psr', 'dsr', 'pbo', 'data_snooping_probability', 'fdr_q_value'])
                   OR NEW.sample_count <> jsonb_array_length(NEW.raw_output->'path_returns')
                THEN RAISE EXCEPTION 'multiple-testing evaluator output is incomplete'; END IF;
            END IF;
            expected_hash := analysis.research_evaluator_output_hash_v2(
                NEW.research_trial_id, NEW.trial_result_id, NEW.analysis_run_id,
                NEW.evidence_kind, NEW.evaluator_id, NEW.evaluator_code_version,
                NEW.input_hash, NEW.universe_hash, NEW.feature_hash,
                NEW.sample_count, NEW.domain_valid, NEW.raw_output, NEW.available_at
            );
            expected_signature := encode(public.hmac(
                convert_to(analysis.research_evaluator_signature_payload(
                    NEW.research_trial_id, NEW.trial_result_id, NEW.analysis_run_id,
                    NEW.evidence_kind, NEW.evaluator_id, NEW.evaluator_code_version,
                    NEW.input_hash, NEW.universe_hash, NEW.feature_hash,
                    NEW.sample_count, NEW.domain_valid, expected_hash, NEW.available_at
                ), 'UTF8'), convert_to(signing_key, 'UTF8'), 'sha256'::TEXT
            ), 'hex');
            IF lower(NEW.output_hash) <> expected_hash OR lower(NEW.signature) <> expected_signature THEN
                RAISE EXCEPTION 'evaluator output signature or content hash is invalid';
            END IF;
            NEW.created_at := NEW.available_at;
            NEW.output_hash := expected_hash;
            NEW.signature := expected_signature;
            RETURN NEW;
        END;
        $_$;

ALTER FUNCTION analysis.enforce_research_evaluator_output() OWNER TO market_research_signer;

CREATE FUNCTION analysis.enforce_research_evidence_manifest() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
        DECLARE source analysis.research_evaluator_output%ROWTYPE;
            result_trial UUID; result_hash TEXT; result_outcome JSONB;
            actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'research evidence manifests are immutable';
            END IF;
            SELECT research_trial_id, input_hash, outcome INTO result_trial, result_hash, result_outcome
            FROM analysis.trial_result WHERE id = NEW.trial_result_id;
            SELECT * INTO source FROM analysis.research_evaluator_output
            WHERE id = NEW.evaluator_output_id;
            IF source.id IS NULL
               OR source.research_trial_id <> NEW.research_trial_id
               OR source.trial_result_id <> NEW.trial_result_id
               OR source.evidence_kind <> NEW.evidence_kind
               OR source.evaluator_id IS DISTINCT FROM NEW.evaluator_id
               OR source.evaluator_code_version IS DISTINCT FROM NEW.evaluator_code_version
               OR source.input_hash IS DISTINCT FROM NEW.input_hash
               OR source.universe_hash IS DISTINCT FROM NEW.universe_hash
               OR source.feature_hash IS DISTINCT FROM NEW.feature_hash
               OR source.output_hash IS DISTINCT FROM NEW.evidence_hash
               OR NEW.payload IS DISTINCT FROM source.raw_output
               OR result_trial IS NULL OR result_trial <> NEW.research_trial_id
               OR result_hash IS DISTINCT FROM source.input_hash
               OR result_outcome->>'passed' IS DISTINCT FROM 'true'
            THEN
                RAISE EXCEPTION 'research evidence must bind exactly to an immutable evaluator output';
            END IF;
            IF NEW.evidence_kind = 'controls'
               AND (NEW.payload->'randomized_label_samples' IS DISTINCT FROM result_outcome->'checks'->'negative_controls'->'randomized_label_samples'
                    OR NEW.payload->'white_noise_samples' IS DISTINCT FROM result_outcome->'checks'->'negative_controls'->'white_noise_samples')
            THEN RAISE EXCEPTION 'control validation differs from evaluator output';
            ELSIF NEW.evidence_kind = 'cpcv_paths'
               AND (NEW.payload->>'path_count' IS DISTINCT FROM result_outcome->'checks'->'combinatorial_paths'->>'path_count'
                    OR NEW.payload->'path_records' IS DISTINCT FROM result_outcome->'checks'->'combinatorial_paths'->'path_records')
            THEN RAISE EXCEPTION 'CPCV validation differs from evaluator output';
            ELSIF NEW.evidence_kind = 'neutralization'
               AND NEW.payload->'samples' IS DISTINCT FROM result_outcome->'checks'->'neutralization'->'samples'
            THEN RAISE EXCEPTION 'neutralization validation differs from evaluator output';
            ELSIF NEW.evidence_kind = 'parameter_stability'
               AND NEW.payload->'samples' IS DISTINCT FROM result_outcome->'checks'->'parameter_stability'->'samples'
            THEN RAISE EXCEPTION 'stability validation differs from evaluator output';
            ELSIF NEW.evidence_kind = 'mechanism_falsification'
               AND NEW.payload->'samples' IS DISTINCT FROM result_outcome->'checks'->'mechanism'->'evidence_samples'
            THEN RAISE EXCEPTION 'mechanism validation differs from evaluator output';
            ELSIF NEW.evidence_kind = 'multiple_testing'
               AND (NEW.payload->'path_returns' IS DISTINCT FROM result_outcome->'checks'->'multiple_testing'->'path_returns'
                    OR NEW.payload->'p_values' IS DISTINCT FROM result_outcome->'checks'->'multiple_testing'->'p_values'
                    OR NEW.payload->'metrics' IS DISTINCT FROM result_outcome->'checks'->'multiple_testing')
            THEN RAISE EXCEPTION 'multiple-testing validation differs from evaluator output';
            END IF;
            -- Preserve the old domain checks, but compare their values with
            -- the source output instead of trusting a validation projection.
            IF NEW.sample_count <> source.sample_count OR NOT NEW.domain_valid OR NOT source.domain_valid THEN
                RAISE EXCEPTION 'research evidence source domain is invalid';
            END IF;
            NEW.created_at := actual;
            NEW.available_at := actual;
            NEW.evidence_hash := source.output_hash;
            RETURN NEW;
        END;
        $$;

ALTER FUNCTION analysis.enforce_research_evidence_manifest() OWNER TO market_research_signer;

CREATE FUNCTION analysis.enforce_research_gate_actual_availability() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            NEW.evaluated_at := actual;
            NEW.available_at := actual;
            RETURN NEW;
        END;
        $$;

ALTER FUNCTION analysis.enforce_research_gate_actual_availability() OWNER TO market_research_signer;

CREATE FUNCTION analysis.enforce_research_gate_pit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE cutoff TIMESTAMPTZ;
        BEGIN
            SELECT trial.input_cutoff INTO cutoff
            FROM analysis.validation_dossier dossier
            JOIN analysis.research_trial trial ON trial.id = dossier.research_trial_id
            WHERE dossier.id = NEW.dossier_id;
            IF cutoff IS NOT NULL AND NEW.available_at > NEW.evaluated_at THEN
                RAISE EXCEPTION 'validation gate has invalid actual availability';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_gate_promotion_clock() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.status = 'active' AND (NEW.research_required OR NEW.hypothesis_id IS NOT NULL OR NEW.experiment_family_id IS NOT NULL) AND EXISTS (
                SELECT 1
                FROM analysis.validation_dossier dossier
                JOIN analysis.research_trial trial ON trial.id = dossier.research_trial_id
                JOIN analysis.validation_gate_result gate ON gate.dossier_id = dossier.id
                WHERE dossier.strategy_revision_id = NEW.id
                  AND (gate.evaluated_at > trial.input_cutoff OR gate.available_at > trial.input_cutoff)
            ) THEN
                RAISE EXCEPTION 'promotion requires database-authoritative gate timestamps at or before trial cutoff';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_manifest_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'research manifests are immutable';
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_result_actual_availability() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            IF NEW.observed_at > actual OR NEW.available_at > actual THEN
                RAISE EXCEPTION 'trial result observed or available timestamp cannot be future-dated';
            END IF;
            -- These are evidence-publication timestamps. A caller cannot
            -- backdate them to make a historical result appear PIT-available.
            NEW.observed_at := actual;
            NEW.available_at := actual;
            RETURN NEW;
        END;
        $$;

ALTER FUNCTION analysis.enforce_research_result_actual_availability() OWNER TO market_research_signer;

CREATE FUNCTION analysis.enforce_research_result_pit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE cutoff TIMESTAMPTZ;
        BEGIN
            SELECT input_cutoff INTO cutoff FROM analysis.research_trial WHERE id = NEW.research_trial_id;
            IF cutoff IS NULL OR NEW.available_at > NEW.observed_at THEN
                RAISE EXCEPTION 'research result has invalid actual availability';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_revision_promotion() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        BEGIN
            IF NEW.status = 'active' AND (
                NEW.research_required OR NEW.hypothesis_id IS NOT NULL OR
                NEW.experiment_family_id IS NOT NULL
            ) THEN
                IF NEW.hypothesis_id IS NULL OR NEW.experiment_family_id IS NULL THEN
                    RAISE EXCEPTION 'research strategy promotion requires hypothesis and experiment family lineage';
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM analysis.validation_dossier dossier
                    JOIN analysis.research_trial trial ON trial.id = dossier.research_trial_id
                    JOIN analysis.experiment_family family
                      ON family.id = trial.experiment_family_id
                     AND family.hypothesis_id = NEW.hypothesis_id
                    JOIN analysis.experiment_manifest experiment_manifest
                      ON experiment_manifest.experiment_family_id = family.id
                    JOIN analysis.strategy_evaluation evaluation
                      ON evaluation.strategy_revision_id = NEW.id
                     AND evaluation.evaluation_type = 'out_of_sample'
                     AND evaluation.verdict = 'pass'
                     AND evaluation.research_trial_id = trial.id
                     AND evaluation.validation_dossier_id = dossier.id
                     AND evaluation.artifact_id = NEW.artifact_id
                     AND evaluation.artifact_hash = NEW.artifact_hash
                      AND evaluation.input_hash = NEW.parameters->>'input_hash'
                      AND evaluation.evaluated_at <= trial.input_cutoff
                    WHERE dossier.strategy_revision_id = NEW.id
                      AND dossier.status = 'sealed'
                      AND trial.status = 'succeeded'
                      AND trial.experiment_family_id = NEW.experiment_family_id
                      AND dossier.artifact_id = NEW.artifact_id
                      AND dossier.artifact_hash = NEW.artifact_hash
                      AND dossier.compiled_policy->>'paper_only' = 'true'
                      AND trial.available_at <= trial.input_cutoff
                      AND dossier.sealed_at <= trial.input_cutoff
                      AND experiment_manifest.available_at <= trial.input_cutoff
                      AND lower(experiment_manifest.manifest_hash) = encode(
                          digest(replace(experiment_manifest.expected_trial_keys::text, ' ', ''), 'sha256'), 'hex'
                      )
                      AND analysis.research_family_complete(trial.experiment_family_id)
                      AND NOT EXISTS (
                          SELECT 1 FROM analysis.research_trial family_trial
                          LEFT JOIN analysis.trial_result family_result
                            ON family_result.research_trial_id = family_trial.id
                           AND family_result.result_kind = 'validation'
                          WHERE family_trial.experiment_family_id = trial.experiment_family_id
                            AND (family_trial.available_at > trial.input_cutoff
                                 OR family_trial.finished_at > trial.input_cutoff
                                 OR family_result.available_at > trial.input_cutoff)
                      )
                      AND analysis.research_trial_universe_complete(trial.id)
                      AND NOT EXISTS (
                          SELECT 1 FROM analysis.trial_universe_manifest manifest
                          WHERE manifest.research_trial_id = trial.id
                            AND (manifest.available_at > trial.input_cutoff
                                 OR lower(manifest.manifest_hash) <> encode(
                                     digest(replace(manifest.expected_members::text, ' ', ''), 'sha256'), 'hex'
                                 ))
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM analysis.universe_observation observation
                          WHERE observation.research_trial_id = trial.id
                            AND observation.available_at > trial.input_cutoff
                      )
                      AND EXISTS (
                          SELECT 1 FROM analysis.trial_result result
                          WHERE result.id = (
                              SELECT candidate.id FROM analysis.trial_result candidate
                              WHERE candidate.research_trial_id = trial.id AND candidate.result_kind = 'validation'
                              ORDER BY candidate.result_version DESC LIMIT 1
                          )
                            AND result.available_at <= trial.input_cutoff
                            AND result.outcome->>'passed' = 'true'
                            AND analysis.research_validation_evidence_complete(result.id, experiment_manifest.expected_trial_count)
                            AND result.outcome->'checks' ? 'multiple_testing'
                            AND result.outcome->'checks' ? 'cost_capacity'
                            AND result.outcome->'checks'->'multiple_testing'->>'domain_valid' = 'true'
                            AND result.outcome->'checks'->'multiple_testing'->>'paths_domain_valid' = 'true'
                      AND result.outcome->'checks'->'multiple_testing'->>'p_values_domain_valid' = 'true'
                            AND (result.outcome->'checks'->'multiple_testing'->>'trials_tested')::integer = experiment_manifest.expected_trial_count
                            AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'psr') = 'number'
                            AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'dsr') = 'number'
                            AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'pbo') = 'number'
                            AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'data_snooping_probability') = 'number'
                            AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'fdr_q_value') = 'number'
                            AND (result.outcome->'checks'->'multiple_testing'->>'psr')::double precision BETWEEN 0 AND 1
                            AND (result.outcome->'checks'->'multiple_testing'->>'dsr')::double precision BETWEEN 0 AND 1
                            AND (result.outcome->'checks'->'multiple_testing'->>'pbo')::double precision BETWEEN 0 AND 1
                            AND (result.outcome->'checks'->'multiple_testing'->>'data_snooping_probability')::double precision BETWEEN 0 AND 1
                            AND (result.outcome->'checks'->'multiple_testing'->>'fdr_q_value')::double precision BETWEEN 0 AND 1
                            AND (result.outcome->'checks'->'cost_capacity'->'multiples'->'1x'->>'net_return') IS NOT NULL
                            AND (result.outcome->'checks'->'cost_capacity'->'multiples'->'2x'->>'net_return') IS NOT NULL
                            AND (result.outcome->'checks'->'cost_capacity'->'multiples'->'3x'->>'net_return') IS NOT NULL
                            AND jsonb_typeof(result.outcome->'checks'->'cost_capacity'->'multiples'->'1x'->'net_return') = 'number'
                            AND jsonb_typeof(result.outcome->'checks'->'cost_capacity'->'multiples'->'2x'->'net_return') = 'number'
                            AND jsonb_typeof(result.outcome->'checks'->'cost_capacity'->'multiples'->'3x'->'net_return') = 'number'
                      )
                      AND (SELECT count(*) FROM analysis.validation_gate_result gate
                           WHERE gate.dossier_id = dossier.id
                             AND gate.verdict = 'pass'
                             AND gate.metrics->>'passed' = 'true'
                             AND gate.metrics->>'domain_valid' = 'true'
                             AND jsonb_typeof(gate.metrics->'checks') = 'object'
                             AND gate.metrics->'checks' <> '{}'::jsonb
                             AND CASE gate.gate_code
                                 WHEN 'pit_integrity' THEN gate.metrics->'checks' ? 'pit'
                                 WHEN 'denominator_completeness' THEN gate.metrics->'checks' ?& ARRAY['denominator', 'attempt_manifest']
                                 WHEN 'oos_predictive_validity' THEN gate.metrics->'checks' ?& ARRAY['predictive', 'multiple_testing']
                                 WHEN 'falsification_and_robustness' THEN gate.metrics->'checks' ?& ARRAY['mechanism', 'negative_controls', 'parameter_stability', 'neutralization', 'combinatorial_paths', 'robustness']
                                 WHEN 'economic_promotability' THEN gate.metrics->'checks' ?& ARRAY['cost_capacity', 'neutralization']
                                 ELSE false
                             END
                             AND gate.available_at <= trial.input_cutoff
                             AND gate.evidence->>'trial_result_id' = (
                                 SELECT result.id::text FROM analysis.trial_result result
                                 WHERE result.research_trial_id = trial.id AND result.result_kind = 'validation'
                                 ORDER BY result.result_version DESC LIMIT 1
                             )) = 5
                      AND EXISTS (
                          SELECT 1
                          FROM analysis.strategy_forecast forecast
                          WHERE forecast.strategy_evaluation_id = evaluation.id
                            AND forecast.strategy_revision_id = NEW.id
                            AND forecast.status = 'available'
                            AND forecast.input_cutoff = trial.input_cutoff
                            AND forecast.as_of = trial.input_cutoff
                            AND forecast.model_artifact_id = NEW.artifact_id
                            AND forecast.target = evaluation.metrics->>'target'
                            AND forecast.artifact_hash = NEW.artifact_hash
                            AND forecast.input_hash = evaluation.input_hash
                            AND forecast.generated_at <= trial.input_cutoff
                            AND forecast.available_at <= trial.input_cutoff
                            AND forecast.forecast_distribution IS NOT NULL
                            AND EXISTS (
                                SELECT 1 FROM jsonb_array_elements(evaluation.metrics->'forecasts') item
                                WHERE item->>'horizon' = forecast.horizon
                                  AND (item->>'forecast_value')::double precision IS NOT DISTINCT FROM forecast.forecast_value
                                  AND item->'forecast_distribution' = forecast.forecast_distribution
                                  AND (item->>'probability_semantics') IS NOT DISTINCT FROM forecast.probability_semantics
                            )
                      )
                      AND jsonb_typeof(evaluation.metrics->'forecasts') = 'array'
                      AND jsonb_array_length(evaluation.metrics->'forecasts') >= 1
                      AND (SELECT count(*)
                           FROM analysis.strategy_forecast forecast
                           WHERE forecast.strategy_evaluation_id = evaluation.id
                             AND forecast.strategy_revision_id = NEW.id
                             AND forecast.status = 'available'
                             AND forecast.input_cutoff = trial.input_cutoff
                             AND forecast.as_of = trial.input_cutoff
                            AND forecast.model_artifact_id = NEW.artifact_id
                            AND forecast.target = evaluation.metrics->>'target'
                             AND forecast.artifact_hash = NEW.artifact_hash
                             AND forecast.input_hash = evaluation.input_hash
                      ) = (SELECT expected_member_count
                           FROM analysis.trial_universe_manifest universe_manifest
                           WHERE universe_manifest.research_trial_id = trial.id)
                            * jsonb_array_length(evaluation.metrics->'forecasts')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM jsonb_array_elements_text((SELECT expected_members
                                                          FROM analysis.trial_universe_manifest universe_manifest
                                                          WHERE universe_manifest.research_trial_id = trial.id)) expected(member)
                          CROSS JOIN jsonb_array_elements(evaluation.metrics->'forecasts') item
                          WHERE NOT EXISTS (
                              SELECT 1 FROM analysis.strategy_forecast forecast
                              WHERE forecast.strategy_evaluation_id = evaluation.id
                                AND forecast.strategy_revision_id = NEW.id
                                AND forecast.instrument_id::text = expected.member
                                AND forecast.horizon = item->>'horizon'
                                AND forecast.input_cutoff = trial.input_cutoff
                                AND forecast.status = 'available'
                          )
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM jsonb_array_elements(evaluation.metrics->'forecasts') item
                          WHERE NOT EXISTS (
                              SELECT 1 FROM analysis.strategy_forecast forecast
                              WHERE forecast.strategy_evaluation_id = evaluation.id
                                AND forecast.strategy_revision_id = NEW.id
                                AND forecast.status = 'available'
                                AND forecast.input_cutoff = trial.input_cutoff
                                AND item->>'horizon' = forecast.horizon
                                AND (item->>'forecast_value')::double precision IS NOT DISTINCT FROM forecast.forecast_value
                                AND item->'forecast_distribution' = forecast.forecast_distribution
                                AND (item->>'probability_semantics') IS NOT DISTINCT FROM forecast.probability_semantics
                          )
                      )
                ) THEN
                    RAISE EXCEPTION 'research strategy promotion requires sealed dossier, complete manifests, evidence-backed five gates, PIT data, and exact forecast lineage';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;

ALTER FUNCTION analysis.enforce_research_revision_promotion() OWNER TO market_research_signer;

CREATE FUNCTION analysis.enforce_research_revision_promotion_hardened() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        DECLARE trial_cutoff TIMESTAMPTZ; dossier_id UUID; evaluation_id UUID; result_id UUID; expected_members INTEGER; forecast_count INTEGER;
        BEGIN
            IF NEW.status <> 'active' OR NOT (NEW.research_required OR NEW.hypothesis_id IS NOT NULL OR NEW.experiment_family_id IS NOT NULL) THEN
                RETURN NEW;
            END IF;
            SELECT dossier.id, trial.input_cutoff, evaluation.id, result.id, universe.expected_member_count
              INTO dossier_id, trial_cutoff, evaluation_id, result_id, expected_members
            FROM analysis.validation_dossier dossier
            JOIN analysis.research_trial trial ON trial.id = dossier.research_trial_id
            JOIN analysis.strategy_evaluation evaluation ON evaluation.validation_dossier_id = dossier.id
                AND evaluation.strategy_revision_id = NEW.id AND evaluation.evaluation_type = 'out_of_sample'
            JOIN analysis.trial_result result ON result.research_trial_id = trial.id AND result.result_kind = 'validation'
            JOIN analysis.trial_universe_manifest universe ON universe.research_trial_id = trial.id
            WHERE dossier.strategy_revision_id = NEW.id
              AND dossier.status = 'sealed'
              AND trial.status = 'succeeded'
              AND trial.experiment_family_id = NEW.experiment_family_id
              AND NEW.hypothesis_id IS NOT NULL
              AND evaluation.verdict = 'pass'
              AND evaluation.artifact_id = NEW.artifact_id
              AND evaluation.artifact_hash = NEW.artifact_hash
              AND evaluation.input_hash = NEW.parameters->>'input_hash'
              AND dossier.artifact_id = evaluation.artifact_id
              AND dossier.artifact_hash = evaluation.artifact_hash
              AND dossier.sealed_at <= trial.input_cutoff
              AND trial.available_at <= trial.input_cutoff
              AND result.available_at <= trial.input_cutoff
            ORDER BY result.result_version DESC LIMIT 1;
            IF dossier_id IS NULL
               OR NOT analysis.research_validation_evidence_complete(result_id, (SELECT expected_trial_count FROM analysis.experiment_manifest manifest JOIN analysis.research_trial trial ON trial.experiment_family_id = manifest.experiment_family_id WHERE trial.id = (SELECT research_trial_id FROM analysis.validation_dossier WHERE id = dossier_id)))
               OR NOT analysis.research_gate_evidence_complete(dossier_id, result_id)
               OR NEW.artifact_id IS NULL OR NEW.artifact_hash IS NULL
               OR expected_members IS NULL
            THEN
                RAISE EXCEPTION 'promotion requires complete evidence-backed research dossier';
            END IF;
            SELECT count(*) INTO forecast_count
            FROM analysis.strategy_forecast forecast
            WHERE forecast.strategy_revision_id = NEW.id
              AND forecast.strategy_evaluation_id = evaluation_id
              AND forecast.status = 'available'
              AND forecast.input_cutoff = trial_cutoff
              AND forecast.as_of = trial_cutoff
              AND forecast.generated_at <= trial_cutoff
              AND forecast.available_at <= trial_cutoff
              AND forecast.artifact_hash = NEW.artifact_hash
              AND forecast.model_artifact_id = NEW.artifact_id
              AND forecast.input_hash = (SELECT input_hash FROM analysis.strategy_evaluation WHERE id = evaluation_id)
              AND EXISTS (SELECT 1 FROM jsonb_array_elements((SELECT metrics->'forecasts' FROM analysis.strategy_evaluation WHERE id = evaluation_id)) item WHERE item->>'horizon' = forecast.horizon AND (item->>'forecast_value')::DOUBLE PRECISION IS NOT DISTINCT FROM forecast.forecast_value AND item->'forecast_distribution' = forecast.forecast_distribution AND item->>'probability_semantics' IS NOT DISTINCT FROM forecast.probability_semantics);
            IF forecast_count <> expected_members * (SELECT jsonb_array_length(metrics->'forecasts') FROM analysis.strategy_evaluation WHERE id = evaluation_id) THEN
                RAISE EXCEPTION 'promotion requires exact persisted model forecast distribution for every universe member';
            END IF;
            RETURN NEW;
        END;
        $$;

ALTER FUNCTION analysis.enforce_research_revision_promotion_hardened() OWNER TO market_research_signer;

CREATE FUNCTION analysis.enforce_research_rows_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'research authority is immutable';
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_trial_terminal_immutability() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status <> 'running' THEN
                    RAISE EXCEPTION 'terminal research trials are immutable';
                END IF;
                RETURN OLD;
            END IF;
            IF TG_OP = 'INSERT' THEN
                NEW.started_at := actual;
                NEW.available_at := actual;
            END IF;
            IF TG_OP = 'UPDATE' AND (
                OLD.status <> 'running'
                OR NEW.experiment_family_id IS DISTINCT FROM OLD.experiment_family_id
                OR NEW.trial_key IS DISTINCT FROM OLD.trial_key
                OR NEW.input_cutoff IS DISTINCT FROM OLD.input_cutoff
                OR NEW.input_hash IS DISTINCT FROM OLD.input_hash
                OR NEW.parameters IS DISTINCT FROM OLD.parameters
                OR NEW.started_at IS DISTINCT FROM OLD.started_at
                OR NEW.available_at IS DISTINCT FROM OLD.available_at
            ) THEN
                RAISE EXCEPTION 'research trial authority is immutable after creation';
            END IF;
            IF NEW.status <> 'running' AND (
                NOT EXISTS (
                    SELECT 1 FROM analysis.trial_result result
                    WHERE result.research_trial_id = NEW.id
                      AND result.result_kind = 'validation'
                )
                OR NOT analysis.research_trial_universe_complete(NEW.id)
                OR NOT EXISTS (
                    SELECT 1 FROM analysis.experiment_manifest manifest
                    WHERE manifest.experiment_family_id = NEW.experiment_family_id
                      AND manifest.expected_trial_keys ? NEW.trial_key
                      AND manifest.expected_trial_count = jsonb_array_length(manifest.expected_trial_keys)
                      AND manifest.expected_trial_count = (
                          SELECT count(DISTINCT expected.key)
                          FROM jsonb_array_elements_text(manifest.expected_trial_keys) expected(key)
                      )
                      AND lower(manifest.manifest_hash) = encode(
                          digest(replace(manifest.expected_trial_keys::text, ' ', ''), 'sha256'), 'hex'
                      )
                    )
            ) THEN
                RAISE EXCEPTION 'terminal research trial requires a validation result and complete universe manifest';
            END IF;
            IF NEW.status <> 'running' THEN
                NEW.finished_at := actual;
            END IF;
            RETURN NEW;
        END;
        $$;

ALTER FUNCTION analysis.enforce_research_trial_terminal_immutability() OWNER TO market_research_signer;

CREATE FUNCTION analysis.enforce_research_universe_actual_availability() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            IF NEW.observed_at > actual OR NEW.available_at > actual THEN
                RAISE EXCEPTION 'universe observation cannot be future-dated';
            END IF;
            NEW.observed_at := actual;
            NEW.available_at := actual;
            RETURN NEW;
        END;
        $$;

ALTER FUNCTION analysis.enforce_research_universe_actual_availability() OWNER TO market_research_signer;

CREATE FUNCTION analysis.enforce_research_universe_pit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE manifest_cutoff TIMESTAMPTZ;
        BEGIN
            SELECT manifest.cutoff INTO manifest_cutoff
            FROM analysis.trial_universe_manifest manifest
            WHERE manifest.research_trial_id = NEW.research_trial_id;
            IF manifest_cutoff IS NULL OR NEW.cutoff <> manifest_cutoff
               OR NEW.observed_at IS NULL OR NEW.available_at IS NULL THEN
                RAISE EXCEPTION 'universe observation has invalid PIT authority';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_strategy_evaluation_availability() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                NEW.available_at := clock_timestamp();
            ELSIF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'strategy evaluation authority is immutable';
            ELSIF NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'strategy evaluation authority is immutable';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_strategy_forecast_authority() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $_$
        DECLARE actual TIMESTAMPTZ := clock_timestamp(); expected_id TEXT; canonical JSONB;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD IS DISTINCT FROM NEW THEN
                    RAISE EXCEPTION 'strategy forecast immutable content cannot be changed';
                END IF;
                RETURN NEW;
            END IF;
            canonical := jsonb_build_array(
                'strategy-forecast.v1',
                (SELECT upper(symbol) FROM catalog.instrument WHERE id = NEW.instrument_id),
                NEW.opportunity_episode_id, NEW.strategy_revision_id::TEXT,
                NEW.strategy_evaluation_id::TEXT, NEW.target, NEW.horizon,
                analysis.canonical_forecast_number(NEW.forecast_value),
                analysis.canonical_forecast_number((NEW.forecast_range->>'low')::DOUBLE PRECISION),
                analysis.canonical_forecast_number((NEW.forecast_range->>'high')::DOUBLE PRECISION),
                COALESCE((SELECT string_agg(format('%s=%s', key, analysis.canonical_forecast_number(value::DOUBLE PRECISION)), '|' ORDER BY key)
                          FROM jsonb_each_text(COALESCE(NEW.forecast_distribution, '{}'::JSONB))), ''),
                NEW.probability_semantics, NEW.model_artifact_id,
                NEW.artifact_hash, NEW.input_hash,
                to_char(NEW.as_of AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00',
                to_char(NEW.input_cutoff AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00',
                to_char(NEW.generated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00',
                to_char(NEW.available_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00'
            );
            expected_id := 'forecast:strategy-forecast:' || left(encode(digest(canonical::TEXT, 'sha256'), 'hex'), 32);
            IF NEW.id IS DISTINCT FROM expected_id
               OR NEW.artifact_hash !~ '^[0-9a-fA-F]{64}$'
               OR NEW.input_hash !~ '^[0-9a-fA-F]{64}$'
               OR lower(NEW.artifact_hash) = repeat('0', 64)
               OR lower(NEW.input_hash) = repeat('0', 64)
               OR NEW.as_of <> NEW.input_cutoff
               OR NEW.generated_at <= date_trunc('day', actual AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
               OR NEW.generated_at > actual
               OR NEW.available_at < NEW.generated_at
               OR NEW.available_at > actual
               OR (NEW.forecast_value IS NULL AND NEW.forecast_range IS NULL AND NEW.forecast_distribution IS NULL)
            THEN
                RAISE EXCEPTION 'strategy forecast requires canonical full-payload identity and authoritative actual availability';
            END IF;
            RETURN NEW;
        END;
        $_$;

ALTER FUNCTION analysis.enforce_strategy_forecast_authority() OWNER TO market_research_signer;

CREATE FUNCTION analysis.enforce_strategy_revision_parameters_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.parameters IS DISTINCT FROM OLD.parameters THEN
                RAISE EXCEPTION 'strategy revision parameters are immutable';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_validation_dossier_seal() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        DECLARE result_id UUID; expected_attempts INTEGER;
        BEGIN
            IF TG_OP = 'DELETE' OR (TG_OP <> 'INSERT' AND OLD.status IN ('sealed', 'rejected')) THEN
                RAISE EXCEPTION 'validation dossiers are immutable';
            END IF;
            IF TG_OP = 'INSERT' THEN
                NEW.created_at := clock_timestamp();
            END IF;
            IF NEW.status <> 'sealed' THEN
                RETURN NEW;
            END IF;
            IF NEW.research_trial_id IS NULL
               OR jsonb_typeof(NEW.sections) <> 'object'
               OR NOT (NEW.sections ?& ARRAY['hypothesis', 'mechanism', 'falsification', 'controls', 'validation', 'economics', 'lineage'])
               OR EXISTS (SELECT 1 FROM jsonb_each_text(NEW.sections) item WHERE item.value IS NULL OR length(trim(item.value)) = 0)
            THEN
                RAISE EXCEPTION 'validation dossier mandatory sections are incomplete';
            END IF;
            SELECT result.id, manifest.expected_trial_count
              INTO result_id, expected_attempts
            FROM analysis.trial_result result
            JOIN analysis.research_trial trial ON trial.id = result.research_trial_id
            JOIN analysis.experiment_manifest manifest ON manifest.experiment_family_id = trial.experiment_family_id
            WHERE result.research_trial_id = NEW.research_trial_id
              AND result.result_kind = 'validation'
            ORDER BY result.result_version DESC LIMIT 1;
            IF result_id IS NULL
               OR NOT analysis.research_validation_evidence_complete(result_id, expected_attempts)
               OR NOT analysis.research_gate_evidence_complete(NEW.id, result_id)
               OR NOT EXISTS (
                   SELECT 1 FROM analysis.research_trial trial
                   WHERE trial.id = NEW.research_trial_id AND trial.status = 'succeeded'
                     AND trial.finished_at IS NOT NULL
                     AND analysis.research_trial_universe_complete(trial.id)
                     AND analysis.research_family_complete(trial.experiment_family_id)
               )
            THEN
                RAISE EXCEPTION 'validation dossier requires complete evidence-backed five gates';
            END IF;
            NEW.sealed_at := clock_timestamp();
            RETURN NEW;
        END;
        $$;

ALTER FUNCTION analysis.enforce_validation_dossier_seal() OWNER TO market_research_signer;

CREATE FUNCTION analysis.insert_phase4_allocation_item(p jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'analysis', 'raw', 'public'
    AS $$
        BEGIN
            INSERT INTO analysis.portfolio_allocation_item
                (allocation_item_id, allocation_id, candidate_id, ticker, strategy_forecast_id, action_id,
                 rank_id, hypothesis_id, disposition, target_weight, current_weight, marginal_book_utility,
                 trace, blockers, funding_source, funding_amount, input_hash, content_hash)
            VALUES (p->>'allocation_item_id', p->>'allocation_id', p->>'candidate_id', p->>'ticker',
                    NULLIF(p->>'strategy_forecast_id', ''), NULLIF(p->>'action_id', ''), NULLIF(p->>'rank_id', ''),
                    NULLIF(p->>'hypothesis_id', '')::uuid, p->>'disposition', (p->>'target_weight')::double precision,
                    (p->>'current_weight')::double precision, (p->>'marginal_book_utility')::double precision,
                    p->'trace', p->'blockers', NULLIF(p->>'funding_source', ''), (p->>'funding_amount')::double precision,
                    p->>'input_hash', p->>'content_hash')
            ON CONFLICT (allocation_item_id) DO NOTHING;
        END;
        $$;

CREATE FUNCTION analysis.insert_phase4_allocation_snapshot(p jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'analysis', 'raw', 'public'
    AS $$
        BEGIN
            INSERT INTO analysis.portfolio_allocation_snapshot
                (allocation_id, as_of, input_cutoff, status, cash_hurdle, forecast_ids, action_ids,
                 strategy_registry_ids, input_hash, content_hash, metadata)
            VALUES (p->>'allocation_id', (p->>'as_of')::timestamptz, (p->>'input_cutoff')::timestamptz,
                    p->>'status', (p->>'cash_hurdle')::double precision, p->'forecast_ids', p->'action_ids',
                    p->'strategy_registry_ids', p->>'input_hash', p->>'content_hash', p->'metadata')
            ON CONFLICT (allocation_id) DO NOTHING;
        END;
        $$;

CREATE FUNCTION analysis.insert_phase4_book_attribution(p jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'analysis', 'app', 'public'
    AS $$
        BEGIN
            INSERT INTO analysis.book_attribution
                (book_attribution_id, allocation_id, allocation_item_id, strategy_forecast_id, hypothesis_id,
                 action_id, rank_id, expression, experiment_id, trial_id, result_id, paper_execution_observation_id,
                 pnl_status, realized_pnl, attribution, input_cutoff, input_hash, content_hash)
            VALUES (p->>'book_attribution_id', p->>'allocation_id', p->>'allocation_item_id', p->>'strategy_forecast_id',
                    (p->>'hypothesis_id')::uuid, p->>'action_id', p->>'rank_id', p->'expression', p->>'experiment_id',
                    (p->>'trial_id')::uuid, (p->>'result_id')::uuid, p->>'paper_execution_observation_id',
                    p->>'pnl_status', (p->>'realized_pnl')::double precision, p->'attribution',
                    (p->>'input_cutoff')::timestamptz, p->>'input_hash', p->>'content_hash')
            ON CONFLICT (book_attribution_id) DO NOTHING;
        END;
        $$;

CREATE FUNCTION analysis.insert_phase4_execution(p_id text, p_allocation_id text, p_model_version text, p_calibration_status text, p_sample_count integer, p_fill_probability double precision, p_spread_bps double precision, p_latency_ms double precision, p_impact_bps double precision, p_input_cutoff timestamp with time zone, p_input_hash text, p_content_hash text, p_metadata jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'analysis', 'app', 'public'
    AS $$
        BEGIN
            INSERT INTO analysis.execution_model_snapshot
                (execution_model_snapshot_id, allocation_id, model_version, calibration_status,
                 sample_count, fill_probability, spread_bps, latency_ms, impact_bps,
                 input_cutoff, input_hash, content_hash, metadata)
            VALUES (p_id, p_allocation_id, p_model_version, p_calibration_status,
                    p_sample_count, p_fill_probability, p_spread_bps, p_latency_ms, p_impact_bps,
                    p_input_cutoff, p_input_hash, p_content_hash, p_metadata)
            ON CONFLICT (execution_model_snapshot_id) DO NOTHING;
        END;
        $$;

CREATE FUNCTION analysis.insert_phase4_paper_execution_observation(p jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'analysis', 'app', 'public'
    AS $$
        BEGIN
            INSERT INTO app.paper_execution_observation
                (paper_execution_observation_id, allocation_item_id, action_id, paper_order_id, execution_mode,
                 paper_only, status, requested_quantity, filled_quantity, requested_price, fill_price, spread_bps,
                 latency_ms, impact_bps, side, exit_price, event_fee, contract_multiplier, observed_at, available_at, metadata)
            VALUES (p->>'paper_execution_observation_id', p->>'allocation_item_id', p->>'action_id',
                    (p->>'paper_order_id')::uuid, p->>'execution_mode', (p->>'paper_only')::boolean, p->>'status',
                    (p->>'requested_quantity')::double precision, (p->>'filled_quantity')::double precision,
                    (p->>'requested_price')::double precision, (p->>'fill_price')::double precision,
                    (p->>'spread_bps')::double precision, (p->>'latency_ms')::double precision,
                    (p->>'impact_bps')::double precision, p->>'side', (p->>'exit_price')::double precision,
                    (p->>'event_fee')::double precision, (p->>'contract_multiplier')::double precision,
                    (p->>'observed_at')::timestamptz, (p->>'available_at')::timestamptz, p->'metadata')
            ON CONFLICT (paper_execution_observation_id) DO NOTHING;
        END;
        $$;

CREATE FUNCTION analysis.insert_phase4_scenario(p_id text, p_allocation_id text, p_model_version text, p_probability_semantics text, p_scenarios jsonb, p_tail_dependence jsonb, p_simultaneous_unwind jsonb, p_input_cutoff timestamp with time zone, p_input_hash text, p_content_hash text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'analysis', 'app', 'public'
    AS $$
        BEGIN
            INSERT INTO analysis.probabilistic_portfolio_scenario_artifact
                (scenario_artifact_id, allocation_id, model_version, probability_semantics,
                 scenarios, tail_dependence, simultaneous_unwind, input_cutoff, input_hash, content_hash)
            VALUES (p_id, p_allocation_id, p_model_version, p_probability_semantics,
                    p_scenarios, p_tail_dependence, p_simultaneous_unwind,
                    p_input_cutoff, p_input_hash, p_content_hash)
            ON CONFLICT (scenario_artifact_id) DO NOTHING;
        END;
        $$;

CREATE FUNCTION analysis.phase3_factor_evidence_valid(result_uuid uuid, result_hash text, evidence_cutoff timestamp with time zone) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$
            SELECT EXISTS (
                SELECT 1
                  FROM analysis.trial_result result
                  JOIN analysis.research_evaluator_output factor_output
                    ON factor_output.trial_result_id = result.id
                   AND factor_output.research_trial_id = result.research_trial_id
                   AND factor_output.evidence_kind = 'neutralization'
                 WHERE result.id = result_uuid
                   AND result.input_hash::TEXT = result_hash
                   AND factor_output.available_at <= evidence_cutoff
                   AND factor_output.raw_output->>'producer' = 'postgresql'
                   AND factor_output.raw_output->>'trial_result_id' = result.id::TEXT
                   AND factor_output.raw_output->>'result_hash' = result.input_hash::TEXT
                   AND jsonb_typeof(factor_output.raw_output->'factor_exposure') = 'object'
                   AND factor_output.raw_output->'factor_exposure' <> '{}'::jsonb
                   AND NOT EXISTS (
                       SELECT 1 FROM jsonb_each(factor_output.raw_output->'factor_exposure') factor
                       WHERE jsonb_typeof(factor.value) <> 'number'
                   )
                   AND jsonb_typeof(factor_output.raw_output->'neutralized_result') = 'object'
                   AND jsonb_typeof(factor_output.raw_output->'neutralized') = 'boolean'
                   AND (factor_output.raw_output->>'neutralized')::BOOLEAN IS TRUE
                   AND factor_output.raw_output->>'factor_exposure_hash' = analysis.phase3_json_hash(factor_output.raw_output->'factor_exposure')
                   AND factor_output.raw_output->>'neutralized_result_hash' = analysis.phase3_json_hash(factor_output.raw_output->'neutralized_result')
            )
        $$;

CREATE FUNCTION analysis.phase3_json_hash(payload jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT
    AS $$
            SELECT encode(public.digest(convert_to(payload::text, 'UTF8'), 'sha256'), 'hex')
        $$;

CREATE FUNCTION analysis.phase4_account_authority_exists(authority_id text, cutoff timestamp with time zone) RETURNS boolean
    LANGUAGE plpgsql STABLE
    AS $_$
BEGIN
  IF authority_id ~ '^broker-account:[0-9]+$' THEN
    RETURN EXISTS (
      SELECT 1 FROM raw.broker_account_snapshot account
       WHERE ('broker-account:' || account.id::TEXT) = authority_id
         AND account.observed_at <= cutoff
    );
  ELSIF authority_id ~ '^manual-account:[0-9]+$' THEN
    RETURN EXISTS (
      SELECT 1 FROM app.manual_account_snapshot account
       WHERE ('manual-account:' || account.id::TEXT) = authority_id
         AND account.reconciliation_state = 'reconciled'
         AND account.effective_at <= cutoff
         AND account.recorded_at <= cutoff
    );
  END IF;
  RETURN FALSE;
END;
$_$;

CREATE FUNCTION analysis.phase4_allocation_authorization_payload(p_snapshot jsonb, p_items jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
          SELECT analysis.phase4_canonical_json(jsonb_build_object(
            'contract', 'phase4-allocation-writer.v1', 'snapshot', p_snapshot, 'items', p_items))
        $$;

CREATE FUNCTION analysis.phase4_allocation_signing_key() RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
          SELECT convert_from(secret, 'UTF8') FROM analysis.phase4_allocation_signing_secret WHERE singleton
        $$;

CREATE FUNCTION analysis.phase4_canonical_json(payload jsonb) RETURNS text
    LANGUAGE plpgsql IMMUTABLE STRICT
    AS $$
        DECLARE kind TEXT; result TEXT;
        BEGIN
            kind := jsonb_typeof(payload);
            IF kind = 'object' THEN
                SELECT COALESCE('{' || string_agg(to_json(key)::text || ': ' || analysis.phase4_canonical_json(value), ', ' ORDER BY length(key), key) || '}', '{}')
                  INTO result FROM jsonb_each(payload);
            ELSIF kind = 'array' THEN
                SELECT COALESCE('[' || string_agg(analysis.phase4_canonical_json(value), ', ' ORDER BY ordinality) || ']', '[]')
                  INTO result FROM jsonb_array_elements(payload) WITH ORDINALITY;
            ELSE
                result := payload::text;
            END IF;
            RETURN result;
        END;
        $$;

CREATE FUNCTION analysis.phase4_canonical_timestamp(value timestamp with time zone) RETURNS jsonb
    LANGUAGE sql IMMUTABLE STRICT
    AS $$
            SELECT to_jsonb(value AT TIME ZONE 'UTC')
        $$;

CREATE FUNCTION analysis.phase4_content_digest(payload jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT
    AS $$
            SELECT encode(public.digest(convert_to(analysis.phase4_canonical_json(payload), 'UTF8'), 'sha256'), 'hex')
        $$;

CREATE FUNCTION analysis.phase4_funding_source_capacity(source_key text, authority_id text, cutoff timestamp with time zone) RETURNS double precision
    LANGUAGE plpgsql STABLE
    AS $_$
DECLARE
  capacity DOUBLE PRECISION;
  snapshot_effective_at TIMESTAMPTZ;
  cash_change DOUBLE PRECISION;
BEGIN
  IF NOT analysis.phase4_account_authority_exists(authority_id, cutoff) THEN
    RETURN NULL;
  END IF;
  IF source_key = 'CASH:' || authority_id THEN
    IF authority_id ~ '^broker-account:[0-9]+$' THEN
      SELECT account.cash_balance::DOUBLE PRECISION INTO capacity
        FROM raw.broker_account_snapshot account
       WHERE ('broker-account:' || account.id::TEXT) = authority_id
         AND account.observed_at <= cutoff;
      RETURN capacity;
    END IF;
    SELECT account.cash_balance::DOUBLE PRECISION, account.effective_at
      INTO capacity, snapshot_effective_at
      FROM app.manual_account_snapshot account
     WHERE ('manual-account:' || account.id::TEXT) = authority_id
       AND account.reconciliation_state = 'reconciled'
       AND account.effective_at <= cutoff
       AND account.recorded_at <= cutoff;
    IF capacity IS NULL OR snapshot_effective_at IS NULL THEN
      RETURN NULL;
    END IF;
    SELECT COALESCE(SUM(
        CASE
          WHEN transaction.transaction_type IN ('cash_deposit', 'dividend', 'sell')
            THEN COALESCE(transaction.amount, 0) - COALESCE(transaction.fees, 0)
          WHEN transaction.transaction_type IN ('cash_withdrawal', 'fee', 'buy')
            THEN -(COALESCE(transaction.amount, 0) + COALESCE(transaction.fees, 0))
          ELSE 0
        END
      ), 0)
      INTO cash_change
      FROM app.portfolio_transaction transaction
     WHERE transaction.executed_at > snapshot_effective_at
       AND transaction.executed_at <= cutoff
       AND transaction.created_at <= cutoff
       AND transaction.reverses_transaction_id IS NULL
       AND NOT EXISTS (
         SELECT 1 FROM app.portfolio_transaction reversal
          WHERE reversal.reverses_transaction_id = transaction.id
            AND reversal.executed_at <= cutoff
            AND reversal.created_at <= cutoff
       );
    capacity := capacity + cash_change;
    SELECT COALESCE(SUM(
        CASE
          WHEN original.transaction_type IN ('cash_deposit', 'dividend', 'sell')
            THEN COALESCE(original.amount, 0) - COALESCE(original.fees, 0)
          WHEN original.transaction_type IN ('cash_withdrawal', 'fee', 'buy')
            THEN -(COALESCE(original.amount, 0) + COALESCE(original.fees, 0))
          ELSE 0
        END
      ), 0)
      INTO cash_change
      FROM app.portfolio_transaction original
      JOIN app.portfolio_transaction reversal
        ON reversal.reverses_transaction_id = original.id
     WHERE original.reverses_transaction_id IS NULL
       AND original.executed_at <= snapshot_effective_at
       AND original.created_at <= cutoff
       AND reversal.executed_at > snapshot_effective_at
       AND reversal.executed_at <= cutoff
       AND reversal.created_at <= cutoff;
    RETURN capacity - cash_change;
  END IF;
  IF source_key ~ '^TRIM:broker-position:[0-9]+$' THEN
    SELECT abs(position.market_value)::DOUBLE PRECISION INTO capacity
      FROM raw.broker_position_snapshot position
      JOIN raw.broker_account_snapshot account ON account.id = position.account_snapshot_id
     WHERE position.id = split_part(source_key, ':', 3)::BIGINT
       AND ('broker-account:' || account.id::TEXT) = authority_id
       AND account.observed_at <= cutoff
       AND position.quantity > 0 AND position.market_value IS NOT NULL;
    RETURN capacity;
  END IF;
  IF source_key ~ '^TRIM:manual-position:[0-9]+$' THEN
    SELECT position.quantity::DOUBLE PRECISION * quote.price::DOUBLE PRECISION INTO capacity
      FROM app.portfolio_position position
      LEFT JOIN LATERAL (
        SELECT price FROM raw.current_price_at(cutoff, ARRAY[position.instrument_id]::BIGINT[])
        LIMIT 1
      ) quote ON TRUE
     WHERE position.instrument_id = split_part(source_key, ':', 3)::BIGINT
       AND position.quantity > 0
       AND NOT EXISTS (
         SELECT 1 FROM app.portfolio_transaction transaction
          WHERE transaction.instrument_id = position.instrument_id
            AND (transaction.executed_at > cutoff OR transaction.created_at > cutoff)
       );
    RETURN capacity;
  END IF;
  RETURN NULL;
END;
$_$;

CREATE FUNCTION analysis.phase4_telemetry_authorization_payload(p_contract text, p_payload jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
        SELECT analysis.phase4_canonical_json(jsonb_build_object('contract', p_contract, 'payload', p_payload))
      $$;

CREATE FUNCTION analysis.phase4_telemetry_authorized(p_contract text, p_payload jsonb, p_signature text) RETURNS boolean
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
      DECLARE key TEXT;
      BEGIN
        key := analysis.phase4_allocation_signing_key();
        RETURN key IS NOT NULL AND length(key) >= 16 AND p_signature = encode(public.hmac(
          convert_to(analysis.phase4_telemetry_authorization_payload(p_contract, p_payload), 'UTF8'),
          convert_to(key, 'UTF8'), 'sha256'), 'hex');
      END $$;

CREATE FUNCTION analysis.promote_phase3_strategy(revision_id bigint) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM analysis.strategy_revision
                 WHERE id = revision_id AND p3_enabled
            ) THEN
                RAISE EXCEPTION 'unknown or non-Phase 3 strategy revision: %', revision_id;
            END IF;
            PERFORM set_config('analysis.phase3_transition', 'canonical', true);
            UPDATE analysis.strategy_revision
               SET status = 'active', promoted_at = clock_timestamp()
             WHERE id = revision_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'unknown Phase 3 strategy revision: %', revision_id;
            END IF;
        END;
        $$;

CREATE FUNCTION analysis.reject_phase2_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN RAISE EXCEPTION 'Phase 2 artifacts are immutable'; END; $$;

CREATE FUNCTION analysis.reject_phase4_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'Phase 4 artifacts are immutable';
        END;
        $$;

CREATE FUNCTION analysis.research_check_complete(checks jsonb, name text) RETURNS boolean
    LANGUAGE plpgsql STABLE
    AS $$
        DECLARE item JSONB := checks -> name;
        BEGIN
            IF name = 'multiple_testing' THEN
                RETURN jsonb_typeof(item) = 'object'
                   AND item->>'domain_valid' = 'true'
                   AND jsonb_typeof(item->'sample_size') = 'number'
                   AND (item->>'sample_size')::INTEGER > 0
                   AND jsonb_typeof(item->'path_count') = 'number'
                   AND (item->>'path_count')::INTEGER > 0
                   AND jsonb_typeof(item->'p_value_count') = 'number'
                   AND (item->>'p_value_count')::INTEGER > 0
                   AND jsonb_typeof(item->'trials_tested') = 'number'
                   AND (item->>'trials_tested')::INTEGER > 0
                   AND (item->>'trials_tested')::INTEGER <= 10000
                   AND (item->>'psr')::DOUBLE PRECISION BETWEEN 0 AND 1
                   AND (item->>'dsr')::DOUBLE PRECISION BETWEEN 0 AND 1
                   AND (item->>'pbo')::DOUBLE PRECISION BETWEEN 0 AND 1
                   AND (item->>'data_snooping_probability')::DOUBLE PRECISION BETWEEN 0 AND 1
                   AND (item->>'fdr_q_value')::DOUBLE PRECISION BETWEEN 0 AND 1;
            END IF;
            IF jsonb_typeof(item) <> 'object'
               OR item->>'passed' <> 'true'
               OR item->>'domain_valid' <> 'true' THEN
                RETURN false;
            END IF;
            IF name = 'pit' THEN
                RETURN jsonb_typeof(item->'future_count') = 'number'
                   AND (item->>'future_count')::INTEGER = 0
                   AND jsonb_typeof(item->'observed_count') = 'number'
                   AND (item->>'observed_count')::INTEGER > 0;
            ELSIF name = 'denominator' THEN
                RETURN jsonb_typeof(item->'expected_count') = 'number'
                   AND jsonb_typeof(item->'observed_count') = 'number'
                   AND (item->>'expected_count')::INTEGER > 0
                   AND (item->>'expected_count')::INTEGER = (item->>'observed_count')::INTEGER;
            ELSIF name = 'attempt_manifest' THEN
                RETURN jsonb_typeof(item->'expected_count') = 'number'
                   AND jsonb_typeof(item->'completed_count') = 'number'
                   AND (item->>'expected_count')::INTEGER > 0
                   AND (item->>'expected_count')::INTEGER = (item->>'completed_count')::INTEGER;
            ELSIF name = 'negative_controls' THEN
                RETURN item->>'controls_present' = 'true'
                   AND jsonb_typeof(item->'randomized_sample_count') = 'number'
                   AND jsonb_typeof(item->'white_noise_sample_count') = 'number'
                   AND (item->>'randomized_sample_count')::INTEGER > 0
                   AND (item->>'white_noise_sample_count')::INTEGER > 0;
            ELSIF name = 'mechanism' THEN
                RETURN length(trim(item->>'mechanism_class')) > 0
                   AND length(trim(item->>'falsification_rule')) > 0
                   AND jsonb_typeof(item->'evidence_count') = 'number'
                   AND (item->>'evidence_count')::INTEGER > 0;
            ELSIF name = 'parameter_stability' THEN
                RETURN jsonb_typeof(item->'sample_size') = 'number'
                   AND (item->>'sample_size')::INTEGER >= 3;
            ELSIF name = 'neutralization' THEN
                RETURN item->>'result_exists' = 'true'
                   AND jsonb_typeof(item->'sample_size') = 'number'
                   AND (item->>'sample_size')::INTEGER > 0;
            ELSIF name = 'combinatorial_paths' THEN
                RETURN analysis.research_combinatorial_paths_complete(item);
            ELSIF name = 'robustness' THEN
                RETURN jsonb_typeof(item->'negative_controls') = 'object'
                   AND item->'negative_controls'->>'passed' = 'true'
                   AND item->'negative_controls'->>'domain_valid' = 'true'
                   AND item->'negative_controls'->>'controls_present' = 'true'
                   AND jsonb_typeof(item->'negative_controls'->'randomized_sample_count') = 'number'
                   AND jsonb_typeof(item->'negative_controls'->'white_noise_sample_count') = 'number'
                   AND (item->'negative_controls'->>'randomized_sample_count')::INTEGER > 0
                   AND (item->'negative_controls'->>'white_noise_sample_count')::INTEGER > 0
                   AND jsonb_typeof(item->'parameter_stability') = 'object'
                   AND item->'parameter_stability'->>'passed' = 'true'
                   AND item->'parameter_stability'->>'domain_valid' = 'true'
                   AND (item->'parameter_stability'->>'sample_size')::INTEGER >= 3
                   AND jsonb_typeof(item->'combinatorial_paths') = 'object'
                   AND analysis.research_combinatorial_paths_complete(item->'combinatorial_paths');
            ELSIF name = 'predictive' THEN
                RETURN jsonb_typeof(item->'metrics') = 'object'
                   AND item->'metrics'->>'domain_valid' = 'true'
                   AND analysis.research_check_complete(jsonb_build_object('multiple_testing', item->'metrics'), 'multiple_testing');
            ELSIF name = 'cost_capacity' THEN
                RETURN jsonb_typeof(item->'multiples') = 'object'
                   AND (item->'multiples') ?& ARRAY['1x', '2x', '3x']
                   AND (item->'multiples'->'1x'->>'net_return') IS NOT NULL
                   AND (item->'multiples'->'2x'->>'net_return') IS NOT NULL
                   AND (item->'multiples'->'3x'->>'net_return') IS NOT NULL
                   AND jsonb_typeof(item->'multiples'->'1x'->'net_return') = 'number'
                   AND jsonb_typeof(item->'multiples'->'2x'->'net_return') = 'number'
                   AND jsonb_typeof(item->'multiples'->'3x'->'net_return') = 'number'
                   AND jsonb_typeof(item->'multiples'->'1x'->'capacity') = 'number'
                   AND jsonb_typeof(item->'multiples'->'2x'->'capacity') = 'number'
                   AND jsonb_typeof(item->'multiples'->'3x'->'capacity') = 'number';
            END IF;
            RETURN false;
        END;
        $$;

CREATE FUNCTION analysis.research_combinatorial_paths_complete(item jsonb) RETURNS boolean
    LANGUAGE plpgsql STABLE
    AS $_$
        DECLARE
            record JSONB; path_id TEXT; seen_ids TEXT[] := ARRAY[]::TEXT[];
            test_folds JSONB; train_folds JSONB; metrics JSONB;
            path_count INTEGER := 0; expected_count INTEGER;
        BEGIN
            IF jsonb_typeof(item) <> 'object'
               OR jsonb_typeof(item->'path_count') <> 'number'
               OR (item->>'path_count')::INTEGER <= 0
               OR jsonb_typeof(item->'path_records') <> 'array'
            THEN
                RETURN false;
            END IF;
            expected_count := (item->>'path_count')::INTEGER;
            FOR record IN SELECT value FROM jsonb_array_elements(item->'path_records') LOOP
                path_id := NULLIF(trim(record->>'path_id'), '');
                test_folds := record->'test_folds';
                train_folds := record->'train_folds';
                metrics := record->'metrics';
                IF path_id IS NULL OR path_id = ANY(seen_ids)
                   OR jsonb_typeof(test_folds) <> 'array'
                   OR jsonb_typeof(train_folds) <> 'array'
                   OR jsonb_array_length(test_folds) = 0
                   OR jsonb_array_length(train_folds) = 0
                   OR jsonb_array_length(test_folds) <> (SELECT count(DISTINCT value) FROM jsonb_array_elements(test_folds))
                   OR jsonb_array_length(train_folds) <> (SELECT count(DISTINCT value) FROM jsonb_array_elements(train_folds))
                   OR EXISTS (
                       SELECT 1 FROM jsonb_array_elements_text(test_folds) test_fold
                       JOIN jsonb_array_elements_text(train_folds) train_fold ON train_fold.value = test_fold.value
                   )
                   OR EXISTS (
                       SELECT 1 FROM jsonb_array_elements_text(test_folds) fold
                       WHERE fold.value !~ '^[0-9]+$'
                   )
                   OR EXISTS (
                       SELECT 1 FROM jsonb_array_elements_text(train_folds) fold
                       WHERE fold.value !~ '^[0-9]+$'
                   )
                   OR jsonb_typeof(metrics) <> 'object'
                   OR metrics->>'domain_valid' <> 'true'
                   OR jsonb_typeof(metrics->'sample_size') <> 'number'
                   OR (metrics->>'sample_size')::INTEGER <= 0
                   OR jsonb_typeof(metrics->'fit_train_count') <> 'number'
                   OR (metrics->>'fit_train_count')::INTEGER <= 0
                   OR jsonb_typeof(metrics->'evaluated_test_count') <> 'number'
                   OR (metrics->>'evaluated_test_count')::INTEGER <= 0
                   OR jsonb_typeof(metrics->'mean_return') <> 'number'
                   OR jsonb_typeof(metrics->'psr') <> 'number'
                   OR (metrics->>'psr')::DOUBLE PRECISION NOT BETWEEN 0 AND 1
                   OR jsonb_typeof(metrics->'p_value') <> 'number'
                   OR (metrics->>'p_value')::DOUBLE PRECISION NOT BETWEEN 0 AND 1
                THEN
                    RETURN false;
                END IF;
                seen_ids := array_append(seen_ids, path_id);
                path_count := path_count + 1;
            END LOOP;
            RETURN path_count = expected_count;
        END;
        $_$;

CREATE FUNCTION analysis.research_evaluator_authorization_payload(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
            SELECT concat_ws(chr(31),
                'research-evaluator-authorization.v1', trial_id::TEXT,
                result_id::TEXT, run_id::TEXT, kind, evaluator, code_version,
                input_digest, universe_digest, feature_digest, samples::TEXT,
                CASE WHEN valid THEN 'true' ELSE 'false' END, output::TEXT
            );
        $$;

ALTER FUNCTION analysis.research_evaluator_authorization_payload(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb) OWNER TO market_research_signer;

CREATE FUNCTION analysis.research_evaluator_output_hash(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
            SELECT encode(digest(jsonb_build_object(
                'contract', 'research-evaluator-output.v1',
                'research_trial_id', trial_id::TEXT,
                'trial_result_id', result_id::TEXT,
                'analysis_run_id', run_id::TEXT,
                'evidence_kind', kind,
                'evaluator_id', evaluator,
                'evaluator_code_version', code_version,
                'input_hash', input_digest,
                'universe_hash', universe_digest,
                'feature_hash', feature_digest,
                'sample_count', samples,
                'domain_valid', valid,
                'raw_output', output
            )::TEXT, 'sha256'), 'hex');
        $$;

CREATE FUNCTION analysis.research_evaluator_output_hash_v2(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb, available timestamp with time zone) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
            SELECT encode(public.digest(jsonb_build_object(
                'contract', 'research-evaluator-output.v2',
                'research_trial_id', trial_id::TEXT,
                'trial_result_id', result_id::TEXT,
                'analysis_run_id', run_id::TEXT,
                'evidence_kind', kind,
                'evaluator_id', evaluator,
                'evaluator_code_version', code_version,
                'input_hash', input_digest,
                'universe_hash', universe_digest,
                'feature_hash', feature_digest,
                'sample_count', samples,
                'domain_valid', valid,
                'raw_output', output,
                'available_at', to_char(available AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || 'Z'
            )::TEXT, 'sha256'), 'hex');
        $$;

ALTER FUNCTION analysis.research_evaluator_output_hash_v2(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb, available timestamp with time zone) OWNER TO market_research_signer;

CREATE FUNCTION analysis.research_evaluator_signature_payload(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output_digest text, available timestamp with time zone) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
            SELECT concat_ws(chr(31),
                'research-evaluator-signature.v1', trial_id::TEXT, result_id::TEXT,
                run_id::TEXT, kind, evaluator, code_version, input_digest,
                universe_digest, feature_digest, samples::TEXT,
                CASE WHEN valid THEN 'true' ELSE 'false' END, output_digest,
                to_char(available AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || 'Z'
            );
        $$;

ALTER FUNCTION analysis.research_evaluator_signature_payload(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output_digest text, available timestamp with time zone) OWNER TO market_research_signer;

CREATE FUNCTION analysis.research_evaluator_signing_key() RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
            SELECT convert_from(secret, 'UTF8')
            FROM analysis.research_evaluator_signing_secret
            WHERE singleton
        $$;

ALTER FUNCTION analysis.research_evaluator_signing_key() OWNER TO market_research_signer;

CREATE FUNCTION analysis.research_evidence_complete(result_uuid uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
            SELECT count(*) = 6 AND bool_and(
                manifest.evaluator_output_id IS NOT NULL
                AND source.id IS NOT NULL
                AND source.output_hash = manifest.evidence_hash
                AND source.output_hash = analysis.research_evaluator_output_hash_v2(
                    source.research_trial_id, source.trial_result_id,
                    source.analysis_run_id, source.evidence_kind,
                    source.evaluator_id, source.evaluator_code_version,
                    source.input_hash, source.universe_hash, source.feature_hash,
                    source.sample_count, source.domain_valid, source.raw_output,
                    source.available_at
                )
                AND source.signature = encode(public.hmac(
                    convert_to(analysis.research_evaluator_signature_payload(
                        source.research_trial_id, source.trial_result_id,
                        source.analysis_run_id, source.evidence_kind,
                        source.evaluator_id, source.evaluator_code_version,
                        source.input_hash, source.universe_hash, source.feature_hash,
                        source.sample_count, source.domain_valid, source.output_hash,
                        source.available_at
                    ), 'UTF8'), convert_to(analysis.research_evaluator_signing_key(), 'UTF8'), 'sha256'::TEXT
                ), 'hex')
                AND manifest.payload = source.raw_output
                AND source.trial_result_id = result_uuid
                AND source.domain_valid AND source.sample_count > 0
            )
            FROM analysis.research_evidence_manifest manifest
            LEFT JOIN analysis.research_evaluator_output source ON source.id = manifest.evaluator_output_id
            WHERE manifest.trial_result_id = result_uuid;
        $$;

ALTER FUNCTION analysis.research_evidence_complete(result_uuid uuid) OWNER TO market_research_signer;

CREATE FUNCTION analysis.research_evidence_hash(trial_id uuid, result_id uuid, kind text, evaluator text, samples integer, valid boolean, evidence jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
            SELECT encode(digest(jsonb_build_object(
                'contract', 'research-evidence.v1',
                'research_trial_id', trial_id::TEXT,
                'trial_result_id', result_id::TEXT,
                'evidence_kind', kind,
                'evaluator_id', evaluator,
                'sample_count', samples,
                'domain_valid', valid,
                'payload', evidence
            )::TEXT, 'sha256'), 'hex');
        $$;

CREATE FUNCTION analysis.research_family_complete(family_id uuid) RETURNS boolean
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
            SELECT EXISTS (
                SELECT 1 FROM analysis.experiment_manifest manifest
                WHERE manifest.experiment_family_id = family_id
                  AND lower(manifest.manifest_hash) = encode(
                      public.digest(convert_to(replace(manifest.expected_trial_keys::text, ' ', ''), 'UTF8'), 'sha256'), 'hex'
                  )
                  AND manifest.expected_trial_count = jsonb_array_length(manifest.expected_trial_keys)
                  AND manifest.expected_trial_count = (
                      SELECT count(DISTINCT expected.key)
                      FROM jsonb_array_elements_text(manifest.expected_trial_keys) expected(key)
                  )
                  AND manifest.expected_trial_count = (
                      SELECT count(*) FROM analysis.research_trial trial
                      WHERE trial.experiment_family_id = family_id
                  )
                  AND manifest.expected_trial_keys = COALESCE((
                      SELECT jsonb_agg(to_jsonb(trial.trial_key) ORDER BY trial.trial_key)
                      FROM analysis.research_trial trial
                      WHERE trial.experiment_family_id = family_id
                  ), '[]'::jsonb)
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis.research_trial trial
                      WHERE trial.experiment_family_id = family_id
                        AND (trial.status = 'running' OR trial.finished_at IS NULL OR NOT EXISTS (
                            SELECT 1 FROM analysis.trial_result result
                            WHERE result.research_trial_id = trial.id
                              AND result.result_kind = 'validation'
                              AND result.outcome ? 'passed'
                        ))
                  )
            );
        $$;

CREATE FUNCTION analysis.research_gate_evidence_complete(dossier_uuid uuid, result_uuid uuid) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$
            SELECT count(*) = 5 AND bool_and(
                gate.verdict = 'pass'
                AND gate.metrics->>'passed' = 'true'
                AND gate.metrics->>'domain_valid' = 'true'
                AND gate.metrics->>'validation_result_id' = result_uuid::TEXT
                AND gate.metrics->>'validation_result_input_hash' = result.input_hash
                AND gate.evidence->>'trial_result_id' = result_uuid::TEXT
                AND gate.evidence->>'input_hash' = result.input_hash
                AND gate.metrics->'evidence_manifest_hashes' = (
                    SELECT jsonb_agg(evidence_hash ORDER BY evidence_kind)
                    FROM analysis.research_evidence_manifest
                    WHERE trial_result_id = result_uuid
                )
                AND jsonb_typeof(gate.metrics->'checks') = 'object'
                AND gate.metrics->'checks' <> '{}'::JSONB
                AND CASE gate.gate_code
                    WHEN 'pit_integrity' THEN gate.metrics->'checks'->'pit' = result.outcome->'checks'->'pit'
                    WHEN 'denominator_completeness' THEN gate.metrics->'checks'->'denominator' = result.outcome->'checks'->'denominator'
                        AND gate.metrics->'checks'->'attempt_manifest' = result.outcome->'checks'->'attempt_manifest'
                    WHEN 'oos_predictive_validity' THEN gate.metrics->'checks'->'predictive' = result.outcome->'checks'->'predictive'
                        AND gate.metrics->'checks'->'multiple_testing' = result.outcome->'checks'->'multiple_testing'
                    WHEN 'falsification_and_robustness' THEN gate.metrics->'checks'->'mechanism' = result.outcome->'checks'->'mechanism'
                        AND gate.metrics->'checks'->'negative_controls' = result.outcome->'checks'->'negative_controls'
                        AND gate.metrics->'checks'->'parameter_stability' = result.outcome->'checks'->'parameter_stability'
                        AND gate.metrics->'checks'->'neutralization' = result.outcome->'checks'->'neutralization'
                        AND gate.metrics->'checks'->'combinatorial_paths' = result.outcome->'checks'->'combinatorial_paths'
                        AND gate.metrics->'checks'->'robustness' = result.outcome->'checks'->'robustness'
                    WHEN 'economic_promotability' THEN gate.metrics->'checks'->'cost_capacity' = result.outcome->'checks'->'cost_capacity'
                        AND gate.metrics->'checks'->'neutralization' = result.outcome->'checks'->'neutralization'
                    ELSE false
                END
            )
            FROM analysis.validation_gate_result gate
            JOIN analysis.trial_result result ON result.id = result_uuid
            JOIN analysis.research_trial trial ON trial.id = result.research_trial_id
            WHERE gate.dossier_id = dossier_uuid;
        $$;

CREATE FUNCTION analysis.research_trial_p3_denominator_complete(trial_uuid uuid) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$
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
               AND NOT EXISTS (
                   SELECT 1 FROM analysis.trial_result result
                   JOIN analysis.research_trial trial ON trial.id = result.research_trial_id
                   WHERE result.research_trial_id = trial_uuid
                     AND result.available_at > trial.input_cutoff
               )
        $$;

CREATE FUNCTION analysis.research_trial_universe_complete(trial_id uuid) RETURNS boolean
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
            SELECT EXISTS (
                SELECT 1
                FROM analysis.trial_universe_manifest manifest
                JOIN analysis.research_trial trial ON trial.id = manifest.research_trial_id
                WHERE manifest.research_trial_id = trial_id
                  AND manifest.cutoff = trial.input_cutoff
                  AND manifest.expected_member_count > 0
                  AND manifest.expected_member_count = jsonb_array_length(manifest.expected_members)
                  AND manifest.expected_member_count = (
                      SELECT count(DISTINCT expected.member)
                      FROM jsonb_array_elements_text(manifest.expected_members) expected(member)
                  )
                  AND lower(manifest.manifest_hash) = encode(
                      public.digest(convert_to(replace(manifest.expected_members::text, ' ', ''), 'UTF8'), 'sha256'), 'hex'
                  )
                  AND manifest.expected_member_count = (
                      SELECT count(*) FROM analysis.universe_observation observation
                      WHERE observation.research_trial_id = trial_id
                        AND observation.cutoff = trial.input_cutoff
                        AND observation.input_hash = trial.input_hash
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis.universe_observation observation
                      WHERE observation.research_trial_id = trial_id
                        AND (observation.cutoff <> trial.input_cutoff
                             OR observation.input_hash <> trial.input_hash)
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM jsonb_array_elements_text(manifest.expected_members) expected(member)
                      WHERE NOT EXISTS (
                          SELECT 1 FROM analysis.universe_observation observation
                          WHERE observation.research_trial_id = trial_id
                            AND observation.instrument_id::text = expected.member
                      )
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis.universe_observation observation
                      WHERE observation.research_trial_id = trial_id
                        AND NOT EXISTS (
                            SELECT 1 FROM jsonb_array_elements_text(manifest.expected_members) expected(member)
                            WHERE expected.member = observation.instrument_id::text
                        )
                  )
            );
        $$;

CREATE FUNCTION analysis.research_validation_evidence_complete(result_uuid uuid, expected_attempt_count integer) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
            SELECT EXISTS (
                SELECT 1 FROM analysis.trial_result result
                JOIN analysis.research_trial trial ON trial.id = result.research_trial_id
                WHERE result.id = result_uuid AND result.result_kind = 'validation'
                  AND result.input_hash = trial.input_hash
                  AND result.outcome->>'passed' = 'true'
                  AND jsonb_typeof(result.outcome->'checks') = 'object'
                  AND analysis.research_check_complete(result.outcome->'checks', 'pit')
                  AND analysis.research_check_complete(result.outcome->'checks', 'denominator')
                  AND analysis.research_check_complete(result.outcome->'checks', 'attempt_manifest')
                  AND analysis.research_check_complete(result.outcome->'checks', 'negative_controls')
                  AND analysis.research_check_complete(result.outcome->'checks', 'mechanism')
                  AND analysis.research_check_complete(result.outcome->'checks', 'parameter_stability')
                  AND analysis.research_check_complete(result.outcome->'checks', 'neutralization')
                  AND analysis.research_check_complete(result.outcome->'checks', 'combinatorial_paths')
                  AND analysis.research_check_complete(result.outcome->'checks', 'robustness')
                  AND analysis.research_check_complete(result.outcome->'checks', 'predictive')
                  AND analysis.research_check_complete(result.outcome->'checks', 'multiple_testing')
                  AND analysis.research_check_complete(result.outcome->'checks', 'cost_capacity')
                  AND (result.outcome->'checks'->'multiple_testing'->>'trials_tested')::INTEGER = expected_attempt_count
                  AND analysis.research_trial_universe_complete(trial.id)
                  AND analysis.research_family_complete(trial.experiment_family_id)
                  AND analysis.research_evidence_complete(result.id)
            );
        $$;

ALTER FUNCTION analysis.research_validation_evidence_complete(result_uuid uuid, expected_attempt_count integer) OWNER TO market_research_signer;

CREATE FUNCTION analysis.stamp_agent_task_payload_availability() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          IF TG_OP = 'INSERT' THEN
            NEW.result_available_at := CASE WHEN NEW.result IS NULL THEN NULL ELSE now() END;
            NEW.validation_available_at := CASE WHEN NEW.validation IS NULL THEN NULL ELSE now() END;
            RETURN NEW;
          END IF;
          IF OLD.result IS NOT NULL AND NEW.result IS NULL THEN
            RAISE EXCEPTION 'published agent result cannot be cleared';
          END IF;
          IF OLD.validation IS NOT NULL AND NEW.validation IS NULL THEN
            RAISE EXCEPTION 'published agent validation cannot be cleared';
          END IF;
          NEW.result_available_at := CASE
            WHEN OLD.result_available_at IS NOT NULL THEN OLD.result_available_at
            WHEN NEW.result IS NOT NULL THEN now()
            ELSE NULL
          END;
          NEW.validation_available_at := CASE
            WHEN OLD.validation_available_at IS NOT NULL THEN OLD.validation_available_at
            WHEN NEW.validation IS NOT NULL THEN now()
            ELSE NULL
          END;
          RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.write_phase4_allocation(p_snapshot jsonb, p_items jsonb, p_authorization_signature text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'raw', 'public'
    AS $_$
        DECLARE signing_key TEXT; expected_signature TEXT; account_id BIGINT;
                cutoff TIMESTAMPTZ; expected_hashes JSONB; expected_forecasts JSONB; expected_actions JSONB;
        BEGIN
          signing_key := analysis.phase4_allocation_signing_key();
          IF signing_key IS NULL OR length(signing_key) < 16 THEN
            RAISE EXCEPTION 'Phase 4 allocation signing key is not configured';
          END IF;
          expected_signature := encode(public.hmac(
            convert_to(analysis.phase4_allocation_authorization_payload(p_snapshot, p_items), 'UTF8'),
            convert_to(signing_key, 'UTF8'), 'sha256'::TEXT), 'hex');
          IF p_authorization_signature IS NULL OR lower(p_authorization_signature) <> expected_signature THEN
            RAISE EXCEPTION 'Phase 4 allocation authorization signature is invalid';
          END IF;
          IF jsonb_typeof(p_snapshot) <> 'object' OR jsonb_typeof(p_items) <> 'array'
             OR jsonb_array_length(p_items) = 0 OR p_snapshot->>'authority' IS NOT NULL THEN
            RAISE EXCEPTION 'Phase 4 allocation writer payload is malformed';
          END IF;
          cutoff := (p_snapshot->>'input_cutoff')::timestamptz;
          IF p_snapshot->'metadata'->>'authority' <> 'postgresql'
             OR p_snapshot->'metadata'->>'authority_snapshot_id' !~ '^broker-account:[0-9]+$' THEN
            RAISE EXCEPTION 'Phase 4 allocation authority metadata is invalid';
          END IF;
          account_id := split_part(p_snapshot->'metadata'->>'authority_snapshot_id', ':', 2)::BIGINT;
          IF NOT EXISTS (SELECT 1 FROM raw.broker_account_snapshot account
                         WHERE account.id = account_id AND account.observed_at <= cutoff) THEN
            RAISE EXCEPTION 'Phase 4 allocation authority account is not a PIT PostgreSQL row';
          END IF;
          IF EXISTS (SELECT 1 FROM jsonb_array_elements(p_items) item
                     WHERE item->>'allocation_id' IS DISTINCT FROM p_snapshot->>'allocation_id') THEN
            RAISE EXCEPTION 'Phase 4 allocation items cannot be recombined across snapshots';
          END IF;
          IF EXISTS (
            SELECT 1 FROM jsonb_array_elements(p_items) item
             WHERE item->>'ticker' <> 'CASH' AND item->>'disposition' IN ('selected', 'rollback')
               AND NOT EXISTS (
                 SELECT 1 FROM analysis.ticker_decision decision
                 JOIN analysis.strategy_forecast forecast ON forecast.id = item->>'strategy_forecast_id'
                  WHERE decision.id::text = item->'trace'->>'source_decision_id'
                    AND decision.input_hash = item->'trace'->>'source_decision_input_hash'
                    AND decision.status = 'published' AND decision.published_at IS NOT NULL
                    AND decision.input_manifest->'trade_plan'->>'trade_plan_id' = item->>'action_id'
                    AND decision.input_manifest->'trade_plan'->>'rank_id' = item->>'rank_id'
                    AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = item->>'strategy_forecast_id'
                    AND forecast.input_cutoff <= cutoff AND decision.published_at <= cutoff)) THEN
            RAISE EXCEPTION 'Phase 4 allocation source action lineage is invalid';
          END IF;
          SELECT coalesce(jsonb_agg(DISTINCT item->'trace'->>'source_decision_input_hash' ORDER BY item->'trace'->>'source_decision_input_hash'), '[]'::jsonb),
                 coalesce(jsonb_agg(DISTINCT item->>'strategy_forecast_id' ORDER BY item->>'strategy_forecast_id'), '[]'::jsonb),
                 coalesce(jsonb_agg(DISTINCT item->>'action_id' ORDER BY item->>'action_id'), '[]'::jsonb)
            INTO expected_hashes, expected_forecasts, expected_actions
            FROM jsonb_array_elements(p_items) item
           WHERE item->>'ticker' <> 'CASH' AND item->>'disposition' IN ('selected', 'rollback');
          IF (p_snapshot->>'status' <> 'cash_only' AND (
                coalesce(p_snapshot->'metadata'->'source_hashes', '[]'::jsonb) <> expected_hashes
             OR coalesce(p_snapshot->'forecast_ids', '[]'::jsonb) <> expected_forecasts
             OR coalesce(p_snapshot->'action_ids', '[]'::jsonb) <> expected_actions)) THEN
            RAISE EXCEPTION 'Phase 4 allocation source hashes or identities are not canonical';
          END IF;
          INSERT INTO analysis.portfolio_allocation_snapshot
            (allocation_id, as_of, input_cutoff, status, cash_hurdle, forecast_ids, action_ids,
             strategy_registry_ids, input_hash, content_hash, metadata)
          VALUES (p_snapshot->>'allocation_id', (p_snapshot->>'as_of')::timestamptz,
                  cutoff, p_snapshot->>'status', (p_snapshot->>'cash_hurdle')::double precision,
                  p_snapshot->'forecast_ids', p_snapshot->'action_ids', p_snapshot->'strategy_registry_ids',
                  p_snapshot->>'input_hash', p_snapshot->>'content_hash', p_snapshot->'metadata')
          ON CONFLICT (allocation_id) DO NOTHING;
          INSERT INTO analysis.portfolio_allocation_item
            (allocation_item_id, allocation_id, candidate_id, ticker, strategy_forecast_id, action_id, rank_id,
             hypothesis_id, disposition, target_weight, current_weight, marginal_book_utility, trace, blockers,
             funding_source, funding_amount, funding_sources, input_hash, content_hash)
          SELECT item->>'allocation_item_id', item->>'allocation_id', item->>'candidate_id', item->>'ticker',
                 nullif(item->>'strategy_forecast_id', ''), nullif(item->>'action_id', ''), nullif(item->>'rank_id', ''),
                 nullif(item->>'hypothesis_id', '')::uuid, item->>'disposition', (item->>'target_weight')::double precision,
                 (item->>'current_weight')::double precision, (item->>'marginal_book_utility')::double precision,
                 item->'trace', item->'blockers', nullif(item->>'funding_source', ''),
                 (item->>'funding_amount')::double precision, coalesce(item->'funding_sources', '{}'::jsonb),
                 item->>'input_hash', item->>'content_hash'
            FROM jsonb_array_elements(p_items) item
          ON CONFLICT (allocation_item_id) DO NOTHING;
        END;
        $_$;

CREATE FUNCTION analysis.write_phase4_book_attribution(p jsonb, sig text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'app'
    AS $$
      BEGIN
        IF NOT analysis.phase4_telemetry_authorized('phase4-book-attribution.v1', p, sig) THEN RAISE EXCEPTION 'Phase 4 attribution authorization signature is invalid'; END IF;
        INSERT INTO analysis.book_attribution
          (book_attribution_id, allocation_id, allocation_item_id, strategy_forecast_id, hypothesis_id, action_id, rank_id,
           expression, experiment_id, trial_id, result_id, paper_execution_observation_id, pnl_status, realized_pnl,
           attribution, input_cutoff, input_hash, content_hash)
        VALUES (p->>'book_attribution_id', p->>'allocation_id', p->>'allocation_item_id', p->>'strategy_forecast_id',
          (p->>'hypothesis_id')::uuid, p->>'action_id', p->>'rank_id', jsonb_extract_path(p, 'expression'), p->>'experiment_id',
          (p->>'trial_id')::uuid, (p->>'result_id')::uuid, p->>'paper_execution_observation_id', p->>'pnl_status',
          (p->>'realized_pnl')::double precision, jsonb_extract_path(p, 'attribution'), (p->>'input_cutoff')::timestamptz, p->>'input_hash', p->>'content_hash')
        ON CONFLICT (book_attribution_id) DO NOTHING;
      END $$;

CREATE FUNCTION analysis.write_phase4_execution(p jsonb, sig text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'app', 'public'
    AS $$
        DECLARE
          allocation_cutoff TIMESTAMPTZ;
          complete_cutoff TIMESTAMPTZ;
          target_allocation_id TEXT;
          requested_cutoff TIMESTAMPTZ;
          requested_ids JSONB;
          eligible_ids JSONB;
        BEGIN
          IF NOT analysis.phase4_telemetry_authorized('phase4-execution.v1', p, sig) THEN
            RAISE EXCEPTION 'Phase 4 execution authorization signature is invalid';
          END IF;
          target_allocation_id := p->>'allocation_id';
          requested_cutoff := (p->>'input_cutoff')::TIMESTAMPTZ;
          SELECT input_cutoff INTO allocation_cutoff
            FROM analysis.portfolio_allocation_snapshot snapshot
           WHERE snapshot.allocation_id = target_allocation_id;
          IF allocation_cutoff IS NULL OR requested_cutoff IS NULL THEN
            RAISE EXCEPTION 'Phase 4 execution allocation or cutoff is not persisted';
          END IF;
          IF jsonb_typeof(p->'metadata'->'paper_observation_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Phase 4 execution observations must be an array';
          END IF;
          SELECT coalesce(jsonb_agg(value ORDER BY value), '[]'::jsonb)
            INTO requested_ids
            FROM jsonb_array_elements_text(p->'metadata'->'paper_observation_ids') AS ids(value);
          SELECT coalesce(jsonb_agg(observation.paper_execution_observation_id ORDER BY observation.paper_execution_observation_id), '[]'::jsonb),
                 max(observation.available_at)
            INTO eligible_ids, complete_cutoff
            FROM app.paper_execution_observation observation
            JOIN analysis.portfolio_allocation_item item
              ON item.allocation_item_id = observation.allocation_item_id
            JOIN app.paper_order paper ON paper.id = observation.paper_order_id
           WHERE item.allocation_id = target_allocation_id
             AND observation.action_id = item.action_id
             AND paper.policy_result->>'trade_plan_id' = observation.action_id
             AND observation.available_at > allocation_cutoff
             AND observation.observed_at < observation.available_at
             AND observation.execution_mode = 'paper'
             AND observation.paper_only
             AND observation.status IN ('partial', 'filled', 'partial_exited', 'exited')
             AND observation.filled_quantity > 0
             AND observation.fill_price IS NOT NULL
             AND observation.contract_multiplier IS NOT NULL
             AND observation.event_fee IS NOT NULL
             AND paper.paper_only
             AND paper.submitted_at IS NOT NULL
             AND paper.filled_at IS NOT NULL
             AND paper.fill_evidence_at IS NOT NULL
             AND paper.fill_evidence_at > paper.filled_at
             AND paper.execution_quote IS NOT NULL
             AND paper.fees IS NOT NULL
             AND paper.entry_slippage IS NOT NULL
             AND paper.actual_fill_price IS NOT NULL
             AND paper.filled_quantity > 0
             AND paper.contract_multiplier IS NOT NULL
             AND paper.status IN ('open', 'entered', 'partial_exited', 'exited', 'closed', 'invalidated');
          IF requested_ids IS DISTINCT FROM eligible_ids THEN
            RAISE EXCEPTION 'Phase 4 execution observations must equal the complete eligible allocation fill set';
          END IF;
          IF complete_cutoff IS NOT NULL AND requested_cutoff IS DISTINCT FROM complete_cutoff THEN
            RAISE EXCEPTION 'Phase 4 execution cutoff must equal maximum allocation observation availability';
          END IF;
          PERFORM analysis.write_phase4_execution_0077(p, sig);
        END;
        $$;

CREATE FUNCTION analysis.write_phase4_execution_0077(p jsonb, sig text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'app', 'public'
    AS $$
        DECLARE
          allocation_cutoff TIMESTAMPTZ;
          derived_cutoff TIMESTAMPTZ;
          target_allocation_id TEXT;
          observation_ids JSONB;
          canonical_metadata JSONB;
          sample_count INTEGER;
          derived_status TEXT;
          derived_fill_probability DOUBLE PRECISION;
          derived_spread DOUBLE PRECISION;
          derived_latency DOUBLE PRECISION;
          derived_impact DOUBLE PRECISION;
          expected_id TEXT;
          expected_input_hash TEXT;
          expected_content_hash TEXT;
        BEGIN
          IF NOT analysis.phase4_telemetry_authorized('phase4-execution.v1', p, sig) THEN
            RAISE EXCEPTION 'Phase 4 execution authorization signature is invalid';
          END IF;
          IF jsonb_typeof(p) IS DISTINCT FROM 'object'
             OR p->>'allocation_id' IS NULL
             OR p->>'model_version' IS DISTINCT FROM 'paper-telemetry.v1' THEN
            RAISE EXCEPTION 'Phase 4 execution writer payload is malformed';
          END IF;
          target_allocation_id := p->>'allocation_id';
          SELECT snapshot.input_cutoff INTO allocation_cutoff
            FROM analysis.portfolio_allocation_snapshot snapshot
           WHERE snapshot.allocation_id = target_allocation_id;
          IF allocation_cutoff IS NULL THEN
            RAISE EXCEPTION 'Phase 4 execution snapshot allocation is not persisted';
          END IF;
          observation_ids := coalesce(p->'metadata'->'paper_observation_ids', '[]'::jsonb);
          IF jsonb_typeof(observation_ids) IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Phase 4 execution observations must be an array';
          END IF;
          IF (SELECT count(*) FROM jsonb_array_elements_text(observation_ids))
             <> (SELECT count(DISTINCT value) FROM jsonb_array_elements_text(observation_ids) AS ids(value)) THEN
            RAISE EXCEPTION 'Phase 4 execution observations contain duplicate IDs';
          END IF;
          SELECT count(*)::INTEGER, max(observation.available_at)
            INTO sample_count, derived_cutoff
            FROM jsonb_array_elements_text(observation_ids) AS ids(value)
            JOIN app.paper_execution_observation observation
              ON observation.paper_execution_observation_id = ids.value
            JOIN analysis.portfolio_allocation_item item
              ON item.allocation_item_id = observation.allocation_item_id
           WHERE item.allocation_id = target_allocation_id;
          IF jsonb_array_length(observation_ids) <> sample_count THEN
            RAISE EXCEPTION 'Phase 4 execution observations are not bound to the allocation';
          END IF;
          IF EXISTS (
            SELECT 1
              FROM jsonb_array_elements_text(observation_ids) AS ids(value)
              LEFT JOIN app.paper_execution_observation observation
                ON observation.paper_execution_observation_id = ids.value
              LEFT JOIN analysis.portfolio_allocation_item item
                ON item.allocation_item_id = observation.allocation_item_id
              LEFT JOIN app.paper_order paper ON paper.id = observation.paper_order_id
             WHERE observation.paper_execution_observation_id IS NULL
                OR item.allocation_id IS DISTINCT FROM target_allocation_id
                OR observation.available_at <= allocation_cutoff
                OR observation.available_at > (p->>'input_cutoff')::TIMESTAMPTZ
                OR observation.execution_mode <> 'paper' OR NOT observation.paper_only
                OR observation.status NOT IN ('partial', 'filled', 'partial_exited', 'exited')
                OR observation.filled_quantity <= 0 OR observation.fill_price IS NULL
                OR observation.contract_multiplier IS NULL OR observation.event_fee IS NULL
                OR paper.submitted_at IS NULL OR paper.filled_at IS NULL
                OR paper.fill_evidence_at IS NULL OR paper.execution_quote IS NULL
                OR paper.actual_fill_price IS NULL OR paper.contract_multiplier IS NULL
                OR paper.status NOT IN ('open', 'entered', 'partial_exited', 'exited', 'closed', 'invalidated')
          ) THEN
            RAISE EXCEPTION 'Phase 4 execution snapshot requires genuine post-allocation paper fills';
          END IF;
          IF sample_count > 0 THEN
            derived_status := 'calibrated';
            IF derived_cutoff IS DISTINCT FROM (p->>'input_cutoff')::TIMESTAMPTZ THEN
              RAISE EXCEPTION 'Phase 4 execution cutoff must equal maximum observation availability';
            END IF;
            SELECT avg((observation.filled_quantity > 0)::INTEGER)::DOUBLE PRECISION,
                   avg(observation.spread_bps) FILTER (WHERE observation.spread_bps IS NOT NULL),
                   avg(extract(epoch FROM (paper.filled_at - paper.submitted_at)) * 1000),
                   avg(abs(observation.fill_price - observation.requested_price)
                       / observation.requested_price * 10000)
                     FILTER (WHERE observation.requested_price IS NOT NULL AND observation.requested_price <> 0)
              INTO derived_fill_probability, derived_spread, derived_latency, derived_impact
              FROM jsonb_array_elements_text(observation_ids) AS ids(value)
              JOIN app.paper_execution_observation observation
                ON observation.paper_execution_observation_id = ids.value
              JOIN app.paper_order paper ON paper.id = observation.paper_order_id;
          ELSE
            derived_status := 'calibration_pending';
            derived_cutoff := (p->>'input_cutoff')::TIMESTAMPTZ;
            derived_fill_probability := NULL;
            derived_spread := NULL;
            derived_latency := NULL;
            derived_impact := NULL;
          END IF;
          IF derived_cutoff <= allocation_cutoff THEN
            RAISE EXCEPTION 'Phase 4 execution cutoff must follow its allocation';
          END IF;
          canonical_metadata := jsonb_build_object(
            'paper_observation_ids', (SELECT coalesce(jsonb_agg(value ORDER BY value), '[]'::jsonb)
                                        FROM jsonb_array_elements_text(observation_ids) AS ids(value)),
            'genuine_fill_count', sample_count,
            'source', 'paper_execution_observation');
          IF p->>'calibration_status' IS DISTINCT FROM derived_status
             OR (p->>'sample_count')::INTEGER IS DISTINCT FROM sample_count
             OR (p->>'fill_probability')::DOUBLE PRECISION IS DISTINCT FROM derived_fill_probability
             OR (p->>'spread_bps')::DOUBLE PRECISION IS DISTINCT FROM derived_spread
             OR (p->>'latency_ms')::DOUBLE PRECISION IS DISTINCT FROM derived_latency
             OR (p->>'impact_bps')::DOUBLE PRECISION IS DISTINCT FROM derived_impact
             OR (p->>'input_cutoff')::TIMESTAMPTZ IS DISTINCT FROM derived_cutoff
             OR p->'metadata' IS DISTINCT FROM canonical_metadata THEN
            RAISE EXCEPTION 'Phase 4 execution metrics are not derived from persisted paper fills';
          END IF;
          expected_id := 'execution:' || analysis.phase4_content_digest(jsonb_build_object(
            'allocation_id', target_allocation_id,
            'input_cutoff', analysis.phase4_canonical_timestamp(derived_cutoff),
            'model_version', 'paper-telemetry.v1', 'calibration_status', derived_status,
            'sample_count', sample_count, 'fill_probability', derived_fill_probability,
            'spread_bps', derived_spread, 'latency_ms', derived_latency, 'impact_bps', derived_impact,
            'metadata', canonical_metadata));
          expected_input_hash := split_part(expected_id, ':', 2);
          expected_content_hash := analysis.phase4_content_digest(jsonb_build_object(
            'execution_model_snapshot_id', expected_id, 'allocation_id', target_allocation_id,
            'model_version', 'paper-telemetry.v1', 'calibration_status', derived_status,
            'sample_count', sample_count, 'fill_probability', derived_fill_probability,
            'spread_bps', derived_spread, 'latency_ms', derived_latency, 'impact_bps', derived_impact,
            'input_cutoff', analysis.phase4_canonical_timestamp(derived_cutoff), 'metadata', canonical_metadata));
          IF p->>'execution_model_snapshot_id' IS DISTINCT FROM expected_id
             OR p->>'input_hash' IS DISTINCT FROM expected_input_hash
             OR p->>'content_hash' IS DISTINCT FROM expected_content_hash THEN
            RAISE EXCEPTION 'Phase 4 execution identity or content hash is not database-canonical';
          END IF;
          INSERT INTO analysis.execution_model_snapshot
            (execution_model_snapshot_id, allocation_id, model_version, calibration_status, sample_count,
             fill_probability, spread_bps, latency_ms, impact_bps, input_cutoff, input_hash, content_hash, metadata)
          VALUES (expected_id, target_allocation_id, 'paper-telemetry.v1', derived_status, sample_count,
                  derived_fill_probability, derived_spread, derived_latency, derived_impact,
                  derived_cutoff, expected_input_hash, expected_content_hash, canonical_metadata)
          ON CONFLICT (execution_model_snapshot_id) DO NOTHING;
        END;
        $$;

CREATE FUNCTION analysis.write_phase4_paper_execution(p jsonb, sig text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'app'
    AS $$
      BEGIN
        IF NOT analysis.phase4_telemetry_authorized('phase4-paper-execution.v1', p, sig) THEN RAISE EXCEPTION 'Phase 4 paper execution authorization signature is invalid'; END IF;
        INSERT INTO app.paper_execution_observation
          (paper_execution_observation_id, allocation_item_id, action_id, paper_order_id, execution_mode, paper_only, status,
           requested_quantity, filled_quantity, requested_price, fill_price, spread_bps, latency_ms, impact_bps, side,
           exit_price, event_fee, contract_multiplier, observed_at, available_at, metadata)
        VALUES (p->>'paper_execution_observation_id', p->>'allocation_item_id', p->>'action_id', (p->>'paper_order_id')::uuid,
          p->>'execution_mode', (p->>'paper_only')::boolean, p->>'status', (p->>'requested_quantity')::double precision,
          (p->>'filled_quantity')::double precision, (p->>'requested_price')::double precision, (p->>'fill_price')::double precision,
          (p->>'spread_bps')::double precision, (p->>'latency_ms')::double precision, (p->>'impact_bps')::double precision,
          p->>'side', (p->>'exit_price')::double precision, (p->>'event_fee')::double precision,
          (p->>'contract_multiplier')::double precision, (p->>'observed_at')::timestamptz, (p->>'available_at')::timestamptz, jsonb_extract_path(p, 'metadata'))
        ON CONFLICT (paper_execution_observation_id) DO NOTHING;
      END $$;

CREATE FUNCTION analysis.write_research_evaluator_output(p_trial_id uuid, p_result_id uuid, p_run_id uuid, p_kind text, p_evaluator text, p_code_version text, p_input_digest text, p_universe_digest text, p_feature_digest text, p_samples integer, p_valid boolean, p_output jsonb, p_authorization_signature text) RETURNS TABLE(id uuid, output_hash text, available_at timestamp with time zone)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
        DECLARE
            signing_key TEXT; expected_authorization TEXT;
            existing_output analysis.research_evaluator_output%ROWTYPE;
            actual TIMESTAMPTZ; content_digest TEXT; final_signature TEXT;
        BEGIN
            signing_key := analysis.research_evaluator_signing_key();
            IF signing_key IS NULL OR length(signing_key) < 16 THEN
                RAISE EXCEPTION 'protected research evaluator signing key is not configured';
            END IF;
            expected_authorization := encode(public.hmac(
                convert_to(analysis.research_evaluator_authorization_payload(
                    p_trial_id, p_result_id, p_run_id, p_kind, p_evaluator, p_code_version,
                    p_input_digest, p_universe_digest, p_feature_digest, p_samples,
                    p_valid, p_output
                ), 'UTF8'), convert_to(signing_key, 'UTF8'), 'sha256'::TEXT
            ), 'hex');
            IF p_authorization_signature IS NULL
               OR lower(p_authorization_signature) <> expected_authorization THEN
                RAISE EXCEPTION 'research evaluator authorization signature is invalid';
            END IF;

            SELECT output_row.* INTO existing_output
            FROM analysis.research_evaluator_output output_row
            WHERE output_row.trial_result_id = p_result_id
              AND output_row.evidence_kind = p_kind;
            IF existing_output.id IS NOT NULL THEN
                IF existing_output.research_trial_id IS DISTINCT FROM p_trial_id
                   OR existing_output.analysis_run_id IS DISTINCT FROM p_run_id
                   OR existing_output.evaluator_id IS DISTINCT FROM p_evaluator
                   OR existing_output.evaluator_code_version IS DISTINCT FROM p_code_version
                   OR existing_output.input_hash IS DISTINCT FROM p_input_digest
                   OR existing_output.universe_hash IS DISTINCT FROM p_universe_digest
                   OR existing_output.feature_hash IS DISTINCT FROM p_feature_digest
                   OR existing_output.sample_count IS DISTINCT FROM p_samples
                   OR existing_output.domain_valid IS DISTINCT FROM p_valid
                   OR existing_output.raw_output IS DISTINCT FROM p_output
                THEN
                    RAISE EXCEPTION 'immutable evaluator output conflicts with the protected persisted row';
                END IF;
                RETURN QUERY SELECT existing_output.id, existing_output.output_hash::TEXT, existing_output.available_at;
                RETURN;
            END IF;

            -- The only new timestamp is assigned inside this protected writer.
            actual := clock_timestamp();
            content_digest := analysis.research_evaluator_output_hash_v2(
                p_trial_id, p_result_id, p_run_id, p_kind, p_evaluator,
                p_code_version, p_input_digest, p_universe_digest,
                p_feature_digest, p_samples, p_valid, p_output, actual
            );
            final_signature := encode(public.hmac(
                convert_to(analysis.research_evaluator_signature_payload(
                    p_trial_id, p_result_id, p_run_id, p_kind, p_evaluator,
                    p_code_version, p_input_digest, p_universe_digest,
                    p_feature_digest, p_samples, p_valid, content_digest, actual
                ), 'UTF8'), convert_to(signing_key, 'UTF8'), 'sha256'::TEXT
            ), 'hex');
            RETURN QUERY
            INSERT INTO analysis.research_evaluator_output(
                research_trial_id, trial_result_id, analysis_run_id, evidence_kind,
                evaluator_id, evaluator_code_version, input_hash, universe_hash,
                feature_hash, sample_count, domain_valid, raw_output,
                output_hash, signature, available_at
            ) VALUES (
                p_trial_id, p_result_id, p_run_id, p_kind, p_evaluator,
                p_code_version, p_input_digest, p_universe_digest,
                p_feature_digest, p_samples, p_valid, p_output,
                content_digest, final_signature, actual
            )
            RETURNING analysis.research_evaluator_output.id,
                      analysis.research_evaluator_output.output_hash::TEXT,
                      analysis.research_evaluator_output.available_at;
        END;
        $$;

ALTER FUNCTION analysis.write_research_evaluator_output(p_trial_id uuid, p_result_id uuid, p_run_id uuid, p_kind text, p_evaluator text, p_code_version text, p_input_digest text, p_universe_digest text, p_feature_digest text, p_samples integer, p_valid boolean, p_output jsonb, p_authorization_signature text) OWNER TO market_research_signer;

CREATE FUNCTION app.capture_portfolio_transaction_sector() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.instrument_id IS NOT NULL AND NEW.instrument_sector IS NULL THEN
                SELECT instrument.sector
                  INTO NEW.instrument_sector
                  FROM catalog.instrument instrument
                 WHERE instrument.id = NEW.instrument_id;
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION app.prevent_manual_account_snapshot_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          RAISE EXCEPTION 'manual account snapshots are append-only';
        END;
        $$;

CREATE FUNCTION app.prevent_paper_order_leg_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
          target_order_id uuid;
          expected jsonb;
        BEGIN
          IF TG_OP = 'UPDATE' AND (
            NEW.paper_order_id IS DISTINCT FROM OLD.paper_order_id
            OR NEW.leg_index IS DISTINCT FROM OLD.leg_index
          ) THEN
            RAISE EXCEPTION 'paper order leg identity is immutable';
          END IF;
          target_order_id := CASE WHEN TG_OP = 'INSERT'
            THEN NEW.paper_order_id ELSE OLD.paper_order_id END;
          IF EXISTS (
            SELECT 1 FROM app.paper_order
            WHERE id = target_order_id AND ticket_version IS NOT NULL
          ) THEN
            IF TG_OP = 'INSERT' THEN
              SELECT ticket_snapshot->'legs'->NEW.leg_index
              INTO expected
              FROM app.paper_order
              WHERE id = NEW.paper_order_id;
              IF expected IS NULL OR ROW(
                expected->>'contract_id',
                expected->>'option_type',
                expected->>'side',
                (expected->>'strike')::numeric,
                (expected->>'bid')::numeric,
                (expected->>'ask')::numeric,
                (expected->>'bid_size')::integer,
                (expected->>'ask_size')::integer,
                (expected->>'quote_time')::timestamptz,
                (expected->>'open_interest')::integer,
                (expected->>'volume')::integer
              ) IS DISTINCT FROM ROW(
                NEW.contract_id::text,
                NEW.option_type,
                NEW.side,
                NEW.strike,
                NEW.bid,
                NEW.ask,
                NEW.bid_size,
                NEW.ask_size,
                NEW.quote_time,
                NEW.open_interest,
                NEW.volume
              ) THEN
                RAISE EXCEPTION 'paper order leg does not match immutable ticket';
              END IF;
              RETURN NEW;
            END IF;
            RAISE EXCEPTION 'ticketed paper order legs are append-only';
          END IF;
          RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;

CREATE FUNCTION app.prevent_paper_order_ticket_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          IF OLD.ticket_version IS NOT NULL THEN
            IF TG_OP = 'DELETE' THEN
              RAISE EXCEPTION 'ticketed paper order audit is append-only';
            END IF;
            IF ROW(
              NEW.decision_id, NEW.instrument_id, NEW.side, NEW.quantity,
              NEW.limit_price, NEW.intended_limit_price, NEW.structure,
              NEW.reserved_collateral, NEW.idempotency_key,
              NEW.ticket_version, NEW.ticket_snapshot,
              NEW.policy_result, NEW.policy_snapshot, NEW.lane, NEW.created_at
            ) IS DISTINCT FROM ROW(
              OLD.decision_id, OLD.instrument_id, OLD.side, OLD.quantity,
              OLD.limit_price, OLD.intended_limit_price, OLD.structure,
              OLD.reserved_collateral, OLD.idempotency_key,
              OLD.ticket_version, OLD.ticket_snapshot,
              OLD.policy_result, OLD.policy_snapshot, OLD.lane, OLD.created_at
            ) THEN
              RAISE EXCEPTION 'ticketed paper order intent is immutable';
            END IF;
          END IF;
          RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;

CREATE FUNCTION app.require_complete_paper_order_leg_set() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
          expected_count integer;
          actual_count integer;
        BEGIN
          IF NEW.ticket_version IS NULL THEN
            RETURN NEW;
          END IF;
          expected_count := jsonb_array_length(coalesce(NEW.ticket_snapshot->'legs', '[]'::jsonb));
          SELECT count(*) INTO actual_count
          FROM app.paper_order_leg
          WHERE paper_order_id = NEW.id;
          IF actual_count IS DISTINCT FROM expected_count THEN
            RAISE EXCEPTION 'ticketed paper order requires the complete immutable leg set';
          END IF;
          RETURN NEW;
        END;
        $$;

CREATE FUNCTION ingest.record_source_lifecycle() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'ingest'
    AS $$
        BEGIN
            INSERT INTO ingest.source_lifecycle_history
                (source_id, effective_at, enabled, operational_state)
            VALUES (NEW.id, COALESCE(NULLIF(current_setting('market.phase2_effective_at', true), '')::timestamptz, now()),
                    NEW.enabled, NEW.operational_state)
            ON CONFLICT DO NOTHING;
            RETURN NEW;
        END; $$;

ALTER FUNCTION ingest.record_source_lifecycle() OWNER TO market_migrator;

CREATE FUNCTION ingest.reject_identity_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_TABLE_NAME = 'payload' AND to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
                RAISE EXCEPTION 'ingest payload rows are immutable';
            END IF;
            IF TG_TABLE_NAME = 'payload' AND (
                to_jsonb(NEW)->'id' IS DISTINCT FROM to_jsonb(OLD)->'id' OR
                to_jsonb(NEW)->'run_id' IS DISTINCT FROM to_jsonb(OLD)->'run_id' OR
                to_jsonb(NEW)->'archive_uri' IS DISTINCT FROM to_jsonb(OLD)->'archive_uri' OR
                to_jsonb(NEW)->'sha256' IS DISTINCT FROM to_jsonb(OLD)->'sha256' OR
                to_jsonb(NEW)->'encoding' IS DISTINCT FROM to_jsonb(OLD)->'encoding' OR
                to_jsonb(NEW)->'byte_count' IS DISTINCT FROM to_jsonb(OLD)->'byte_count' OR
                to_jsonb(NEW)->'schema_version' IS DISTINCT FROM to_jsonb(OLD)->'schema_version') THEN
                RAISE EXCEPTION 'ingest payload identity is immutable';
            END IF;
            IF TG_TABLE_NAME = 'run' AND (
                to_jsonb(NEW)->'id' IS DISTINCT FROM to_jsonb(OLD)->'id' OR
                to_jsonb(NEW)->'source_id' IS DISTINCT FROM to_jsonb(OLD)->'source_id' OR
                to_jsonb(NEW)->'source_run_key' IS DISTINCT FROM to_jsonb(OLD)->'source_run_key' OR
                to_jsonb(NEW)->'capability' IS DISTINCT FROM to_jsonb(OLD)->'capability') THEN
                RAISE EXCEPTION 'ingest run identity is immutable';
            END IF;
            RETURN NEW;
        END; $$;

CREATE FUNCTION raw.current_price_at(p_as_of timestamp with time zone, p_instrument_ids bigint[] DEFAULT NULL::bigint[]) RETURNS TABLE(instrument_id bigint, price double precision, change_pct double precision, change_abs double precision, currency text, source_id text, observed_at timestamp with time zone, available_at timestamp with time zone, valuation_status text, source_kind text, trading_date date)
    LANGUAGE plpgsql STABLE
    AS $$
        BEGIN
            IF p_instrument_ids IS NULL THEN
                RETURN QUERY
                SELECT *
                FROM raw.current_price_for_instruments(
                    p_as_of,
                    ARRAY(SELECT id FROM catalog.instrument)
                );
            ELSE
                RETURN QUERY
                SELECT * FROM raw.current_price_for_instruments(p_as_of, p_instrument_ids);
            END IF;
        END
        $$;

CREATE FUNCTION raw.current_price_for_instruments(p_as_of timestamp with time zone, p_instrument_ids bigint[]) RETURNS TABLE(instrument_id bigint, price double precision, change_pct double precision, change_abs double precision, currency text, source_id text, observed_at timestamp with time zone, available_at timestamp with time zone, valuation_status text, source_kind text, trading_date date)
    LANGUAGE sql STABLE
    AS $$
        WITH confirmed_quote AS MATERIALIZED (
            SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.observed_at)
                   fact.*, confirmation.confirmed_at
            FROM (
                SELECT * FROM raw.quote
                WHERE instrument_id = ANY(p_instrument_ids)
                UNION ALL
                SELECT * FROM raw.quote_history
                WHERE instrument_id = ANY(p_instrument_ids)
            ) fact
            
        CROSS JOIN LATERAL (
            SELECT price_run.finished_at AS confirmed_at
            FROM raw.quote_fact_availability availability
            JOIN ingest.run price_run
              ON price_run.id = availability.ingest_run_id
             AND price_run.status IN ('succeeded', 'partial')
             AND price_run.finished_at IS NOT NULL
             AND price_run.finished_at <= p_as_of
            WHERE availability.fact_id = fact.id
              AND availability.fact_available_at = fact.available_at


            ORDER BY confirmed_at
            LIMIT 1
        ) confirmation
    
            WHERE fact.available_at <= p_as_of
            ORDER BY fact.instrument_id, fact.source_id, fact.observed_at,
                     fact.available_at DESC
        ),
        confirmed_daily_bar AS MATERIALIZED (
            SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.trading_date)
                   fact.*, confirmation.confirmed_at
            FROM (
                SELECT * FROM raw.price_bar
                WHERE instrument_id = ANY(p_instrument_ids) AND interval = '1d'
                UNION ALL
                SELECT * FROM raw.price_bar_history
                WHERE instrument_id = ANY(p_instrument_ids) AND interval = '1d'
            ) fact
            
        CROSS JOIN LATERAL (
            SELECT price_run.finished_at AS confirmed_at
            FROM raw.price_bar_fact_availability availability
            JOIN ingest.run price_run
              ON price_run.id = availability.ingest_run_id
             AND price_run.status IN ('succeeded', 'partial')
             AND price_run.finished_at IS NOT NULL
             AND price_run.finished_at <= p_as_of
            WHERE availability.fact_id = fact.id
              AND availability.fact_available_at = fact.available_at


            ORDER BY confirmed_at
            LIMIT 1
        ) confirmation
    
            WHERE fact.available_at <= p_as_of
            ORDER BY fact.instrument_id, fact.source_id, fact.trading_date,
                     fact.available_at DESC, fact.observed_at DESC
        ),
        quote_candidates AS MATERIALIZED (
            SELECT quote.instrument_id,
                   quote.price,
                   quote.change_pct,
                   quote.change_abs,
                   quote.currency,
                   quote.source_id,
                   effective.observed_at,
                   quote.available_at,
                   quote.confirmed_at,
                   CASE
                       WHEN source.kind IN ('daily_bars', 'daily_quote') THEN 'daily_close'::text
                       ELSE 'market_quote'::text
                   END AS valuation_status,
                   source.kind AS source_kind,
                   CASE
                       WHEN source.kind IN ('daily_bars', 'daily_quote')
                           THEN (quote.observed_at AT TIME ZONE 'UTC')::date
                       ELSE (quote.observed_at AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York'))::date
                   END AS trading_date,
                   false AS is_price_bar
            FROM confirmed_quote quote
            JOIN catalog.instrument instrument ON instrument.id = quote.instrument_id
            JOIN ingest.source source ON source.id = quote.source_id AND source.enabled AND source.operational_state = 'active'
            CROSS JOIN LATERAL (
                VALUES (
                    CASE
                        WHEN source.kind IN ('daily_bars', 'daily_quote')
                            THEN ((quote.observed_at AT TIME ZONE 'UTC')::date::timestamp + time '16:00')
                                 AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York')
                        ELSE quote.observed_at
                    END
                )
            ) AS effective(observed_at)
            WHERE quote.price > 0
              AND effective.observed_at <= p_as_of
        ),
        bar_candidates AS MATERIALIZED (
            SELECT bar.instrument_id,
                   bar.close AS price,
                   NULL::double precision AS change_pct,
                   NULL::double precision AS change_abs,
                   'USD'::text AS currency,
                   bar.source_id,
                   ((bar.trading_date::timestamp + time '16:00')
                       AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York')) AS observed_at,
                   bar.available_at,
                   bar.confirmed_at,
                   'daily_close'::text AS valuation_status,
                   source.kind AS source_kind,
                   bar.trading_date,
                   true AS is_price_bar
            FROM confirmed_daily_bar bar
            JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
            JOIN ingest.source source ON source.id = bar.source_id AND source.enabled AND source.operational_state = 'active'
            WHERE bar.close > 0
              AND ((bar.trading_date::timestamp + time '16:00')
                   AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York')) <= p_as_of
        ),
        selected AS MATERIALIZED (
            SELECT DISTINCT ON (candidate.instrument_id) candidate.*
            FROM (
                SELECT * FROM quote_candidates
                UNION ALL
                SELECT * FROM bar_candidates
            ) candidate
            ORDER BY candidate.instrument_id,
                     candidate.confirmed_at DESC,
                     candidate.observed_at DESC,
                     candidate.available_at DESC,
                     CASE candidate.valuation_status WHEN 'market_quote' THEN 0 ELSE 1 END,
                     candidate.source_id
        )
        SELECT selected.instrument_id,
               selected.price,
               CASE
                   WHEN selected.is_price_bar AND previous.close > 0
                       THEN (selected.price / previous.close - 1) * 100
                   ELSE selected.change_pct
               END AS change_pct,
               CASE
                   WHEN selected.is_price_bar AND previous.close IS NOT NULL
                       THEN selected.price - previous.close
                   ELSE selected.change_abs
               END AS change_abs,
               selected.currency,
               selected.source_id,
               selected.observed_at,
               selected.available_at,
               selected.valuation_status,
               selected.source_kind,
               selected.trading_date
        FROM selected
        LEFT JOIN LATERAL (
            SELECT prior.close
            FROM confirmed_daily_bar prior
            WHERE selected.is_price_bar
              AND prior.instrument_id = selected.instrument_id
              AND prior.trading_date < selected.trading_date
            ORDER BY prior.trading_date DESC, prior.available_at DESC,
                     prior.observed_at DESC
            LIMIT 1
        ) previous ON true
        $$;

CREATE FUNCTION raw.project_confirmation_staging() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_TABLE_NAME = 'price_bar_confirmation' THEN
                IF TG_OP = 'UPDATE' THEN
                    DELETE FROM raw.price_bar_fact_availability
                    WHERE fact_id = OLD.fact_id
                      AND fact_available_at = OLD.fact_available_at
                      AND ingest_run_id = OLD.ingest_run_id;
                END IF;
                INSERT INTO raw.price_bar_fact_availability (fact_id, fact_available_at, ingest_run_id)
                VALUES (NEW.fact_id, NEW.fact_available_at, NEW.ingest_run_id)
                ON CONFLICT (fact_id, fact_available_at) DO NOTHING;
            ELSE
                IF TG_OP = 'UPDATE' THEN
                    DELETE FROM raw.quote_fact_availability
                    WHERE fact_id = OLD.fact_id
                      AND fact_available_at = OLD.fact_available_at
                      AND ingest_run_id = OLD.ingest_run_id;
                END IF;
                INSERT INTO raw.quote_fact_availability (fact_id, fact_available_at, ingest_run_id)
                VALUES (NEW.fact_id, NEW.fact_available_at, NEW.ingest_run_id)
                ON CONFLICT (fact_id, fact_available_at) DO NOTHING;
            END IF;
            RETURN NEW;
        END
        $$;

CREATE TABLE analysis.agent_experiment (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    experiment_key text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    champion_provider text NOT NULL,
    champion_model text NOT NULL,
    challenger_provider text NOT NULL,
    challenger_model text NOT NULL,
    max_pairs_per_trading_day integer DEFAULT 12 NOT NULL,
    advisory_only boolean DEFAULT true NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    parameters jsonb DEFAULT '{}'::jsonb NOT NULL,
    immutable_report jsonb,
    report_sealed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_experiment_check CHECK (((status <> 'completed'::text) OR (immutable_report IS NOT NULL))),
    CONSTRAINT agent_experiment_max_pairs_per_trading_day_check CHECK (((max_pairs_per_trading_day >= 1) AND (max_pairs_per_trading_day <= 12))),
    CONSTRAINT agent_experiment_status_check CHECK ((status = ANY (ARRAY['active'::text, 'completed'::text, 'archived'::text])))
);

CREATE TABLE analysis.agent_run (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    trigger text NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    input_tokens bigint,
    output_tokens bigint,
    cost_usd numeric(14,6),
    status text NOT NULL,
    summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    experiment_id uuid,
    arm text,
    evidence_fingerprint text,
    prompt_version text,
    schema_version text,
    baseline_version text,
    validation_status text,
    validation_detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    latency_ms integer
);

CREATE TABLE analysis.agent_task (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_run_id uuid,
    decision_id uuid,
    task_kind text NOT NULL,
    status text NOT NULL,
    request jsonb NOT NULL,
    result jsonb,
    validation jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    result_available_at timestamp with time zone,
    validation_available_at timestamp with time zone,
    experiment_id uuid,
    arm text,
    paired_task_id uuid,
    provider text,
    model text,
    evidence_fingerprint text,
    prompt_version text,
    schema_version text,
    baseline_version text,
    validation_status text,
    validation_detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    latency_ms integer,
    input_tokens bigint,
    output_tokens bigint,
    cost_usd numeric(14,6)
);

CREATE TABLE analysis.book_attribution (
    book_attribution_id text NOT NULL,
    allocation_id text NOT NULL,
    allocation_item_id text NOT NULL,
    strategy_forecast_id text NOT NULL,
    hypothesis_id uuid NOT NULL,
    action_id text NOT NULL,
    rank_id text NOT NULL,
    expression jsonb NOT NULL,
    experiment_id text NOT NULL,
    trial_id uuid NOT NULL,
    result_id uuid NOT NULL,
    paper_execution_observation_id text NOT NULL,
    pnl_status text NOT NULL,
    realized_pnl double precision,
    attribution jsonb NOT NULL,
    input_cutoff timestamp with time zone NOT NULL,
    input_hash character(64) NOT NULL,
    content_hash character(64) NOT NULL,
    available_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT book_attribution_check CHECK (((jsonb_typeof(attribution) = 'object'::text) AND (jsonb_typeof(expression) = 'object'::text) AND (expression <> '{}'::jsonb))),
    CONSTRAINT book_attribution_check1 CHECK (((pnl_status <> 'realized'::text) OR (realized_pnl IS NOT NULL))),
    CONSTRAINT book_attribution_check2 CHECK (((input_hash ~ '^[0-9a-f]{64}$'::text) AND ((input_hash)::text <> repeat('0'::text, 64)) AND (book_attribution_id = ('attribution:'::text || (input_hash)::text)))),
    CONSTRAINT book_attribution_content_hash_check CHECK (((content_hash ~ '^[0-9a-f]{64}$'::text) AND ((content_hash)::text <> repeat('0'::text, 64)))),
    CONSTRAINT book_attribution_pnl_status_check CHECK ((pnl_status = ANY (ARRAY['pending_fill'::text, 'realized'::text, 'unavailable'::text]))),
    CONSTRAINT book_attribution_realized_pnl_check CHECK (((realized_pnl IS NULL) OR ((realized_pnl < 'Infinity'::double precision) AND (realized_pnl > '-Infinity'::double precision))))
);

CREATE TABLE analysis.decision (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    run_id uuid NOT NULL,
    decision_key text NOT NULL,
    kind text NOT NULL,
    instrument_id bigint NOT NULL,
    as_of timestamp with time zone NOT NULL,
    state text NOT NULL,
    rank integer,
    score double precision,
    quality_status text,
    strategy_revision_id bigint,
    reasons text[] DEFAULT '{}'::text[] NOT NULL,
    blockers text[] DEFAULT '{}'::text[] NOT NULL,
    input_hash character(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    lane text,
    episode_key text,
    sample_eligible boolean DEFAULT false NOT NULL,
    quarantine_reason text,
    calibration_cohort text
);

CREATE TABLE analysis.decision_evidence (
    decision_id uuid NOT NULL,
    evidence_kind text NOT NULL,
    reference_key text NOT NULL,
    reference_url text,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE analysis.event_decision_packet (
    event_id text NOT NULL,
    symbol text NOT NULL,
    event_kind text NOT NULL,
    trigger_type text NOT NULL,
    as_of timestamp with time zone NOT NULL,
    publication_id text,
    headline text,
    market_tape jsonb DEFAULT '{}'::jsonb NOT NULL,
    positioning jsonb DEFAULT '{}'::jsonb NOT NULL,
    event_fundamentals jsonb DEFAULT '{}'::jsonb NOT NULL,
    platform_optionality jsonb DEFAULT '{}'::jsonb NOT NULL,
    historical_cases jsonb DEFAULT '{}'::jsonb NOT NULL,
    tactical_decision jsonb DEFAULT '{}'::jsonb NOT NULL,
    fundamental_decision jsonb DEFAULT '{}'::jsonb NOT NULL,
    decision_truth jsonb DEFAULT '{}'::jsonb NOT NULL,
    evidence_refs jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_event_decision_packet_json_objects CHECK (((jsonb_typeof(market_tape) = 'object'::text) AND (jsonb_typeof(positioning) = 'object'::text) AND (jsonb_typeof(event_fundamentals) = 'object'::text) AND (jsonb_typeof(platform_optionality) = 'object'::text) AND (jsonb_typeof(tactical_decision) = 'object'::text) AND (jsonb_typeof(fundamental_decision) = 'object'::text) AND (jsonb_typeof(decision_truth) = 'object'::text)))
);

CREATE TABLE analysis.event_scout_event (
    event_id text NOT NULL,
    symbol text NOT NULL,
    trigger_type text NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    source_url text,
    source_kind text,
    status text NOT NULL,
    cooldown_until timestamp with time zone,
    collection_status jsonb DEFAULT '{}'::jsonb NOT NULL,
    raw jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE analysis.event_study_feature (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    run_id uuid NOT NULL,
    instrument_id bigint,
    market_event_id bigint NOT NULL,
    market_event_version_id bigint NOT NULL,
    as_of timestamp with time zone NOT NULL,
    event_kind text NOT NULL,
    event_session text NOT NULL,
    pre_event_regime text NOT NULL,
    horizon integer NOT NULL,
    sample_size integer NOT NULL,
    actual_move_median double precision,
    actual_move_p75 double precision,
    actual_move_p90 double precision,
    bootstrap_low double precision,
    bootstrap_high double precision,
    win_rate double precision,
    iv_crush_frequency double precision,
    atm_iv double precision,
    skew_25d double precision,
    term_slope double precision,
    implied_move double precision,
    evidence_state text NOT NULL,
    feature_version text NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ck_event_study_evidence_state CHECK ((evidence_state = ANY (ARRAY['ready'::text, 'insufficient_event_evidence'::text, 'unavailable'::text]))),
    CONSTRAINT event_study_feature_horizon_check CHECK ((horizon > 0)),
    CONSTRAINT event_study_feature_sample_size_check CHECK ((sample_size >= 0))
);

CREATE TABLE analysis.execution_model_snapshot (
    execution_model_snapshot_id text NOT NULL,
    allocation_id text NOT NULL,
    model_version text NOT NULL,
    calibration_status text NOT NULL,
    sample_count integer NOT NULL,
    fill_probability double precision,
    spread_bps double precision,
    latency_ms double precision,
    impact_bps double precision,
    input_cutoff timestamp with time zone NOT NULL,
    input_hash character(64) NOT NULL,
    content_hash character(64) NOT NULL,
    available_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT execution_model_snapshot_calibration_status_check CHECK ((calibration_status = ANY (ARRAY['calibrated'::text, 'calibration_pending'::text, 'unavailable'::text]))),
    CONSTRAINT execution_model_snapshot_check CHECK (((calibration_status <> 'calibrated'::text) OR (sample_count > 0))),
    CONSTRAINT execution_model_snapshot_check1 CHECK ((execution_model_snapshot_id = ('execution:'::text || (input_hash)::text))),
    CONSTRAINT execution_model_snapshot_content_hash_check CHECK (((content_hash ~ '^[0-9a-f]{64}$'::text) AND ((content_hash)::text <> repeat('0'::text, 64)))),
    CONSTRAINT execution_model_snapshot_fill_probability_check CHECK (((fill_probability IS NULL) OR ((fill_probability < 'Infinity'::double precision) AND (fill_probability > '-Infinity'::double precision) AND ((fill_probability >= (0)::double precision) AND (fill_probability <= (1)::double precision))))),
    CONSTRAINT execution_model_snapshot_impact_bps_check CHECK (((impact_bps IS NULL) OR ((impact_bps < 'Infinity'::double precision) AND (impact_bps > '-Infinity'::double precision) AND (impact_bps >= (0)::double precision)))),
    CONSTRAINT execution_model_snapshot_input_hash_check CHECK (((input_hash ~ '^[0-9a-f]{64}$'::text) AND ((input_hash)::text <> repeat('0'::text, 64)))),
    CONSTRAINT execution_model_snapshot_latency_ms_check CHECK (((latency_ms IS NULL) OR ((latency_ms < 'Infinity'::double precision) AND (latency_ms > '-Infinity'::double precision) AND (latency_ms >= (0)::double precision)))),
    CONSTRAINT execution_model_snapshot_sample_count_check CHECK ((sample_count >= 0)),
    CONSTRAINT execution_model_snapshot_spread_bps_check CHECK (((spread_bps IS NULL) OR ((spread_bps < 'Infinity'::double precision) AND (spread_bps > '-Infinity'::double precision) AND (spread_bps >= (0)::double precision))))
);

CREATE TABLE analysis.experiment_family (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    hypothesis_id uuid NOT NULL,
    family_key text NOT NULL,
    name text NOT NULL,
    design jsonb DEFAULT '{}'::jsonb NOT NULL,
    controls jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    input_hash character(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    available_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE analysis.experiment_manifest (
    experiment_family_id uuid NOT NULL,
    expected_trial_count integer NOT NULL,
    expected_trial_keys jsonb NOT NULL,
    manifest_hash character(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT experiment_manifest_expected_trial_count_check CHECK (((expected_trial_count >= 1) AND (expected_trial_count <= 10000))),
    CONSTRAINT experiment_manifest_expected_trial_keys_check CHECK ((jsonb_typeof(expected_trial_keys) = 'array'::text))
);

CREATE TABLE analysis.hypothesis (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    hypothesis_key text NOT NULL,
    statement text NOT NULL,
    mechanism_class text NOT NULL,
    falsification text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    input_hash character(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT hypothesis_check CHECK ((available_at >= created_at))
);

CREATE TABLE analysis.market_coverage_vector (
    vector_id text NOT NULL,
    as_of timestamp with time zone NOT NULL,
    status text NOT NULL,
    payload jsonb NOT NULL,
    ingest_run_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    input_content_hash text NOT NULL,
    parent_snapshot_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE analysis.market_scenario_path (
    scenario_hash text NOT NULL,
    snapshot_id text NOT NULL,
    parent_snapshot_id text NOT NULL,
    posterior_id text NOT NULL,
    model_version text NOT NULL,
    ingest_run_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    input_content_hash text NOT NULL,
    path jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE analysis.market_state_posterior (
    posterior_id text NOT NULL,
    as_of timestamp with time zone NOT NULL,
    input_cutoff timestamp with time zone NOT NULL,
    model_version text NOT NULL,
    status text NOT NULL,
    payload jsonb NOT NULL,
    ingest_run_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    input_content_hash text NOT NULL,
    parent_snapshot_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE analysis.option_decision (
    decision_id uuid NOT NULL,
    contract_id bigint NOT NULL,
    snapshot_id bigint NOT NULL,
    quote_observed_at timestamp with time zone NOT NULL,
    primary_decision_id uuid,
    premium_mid double precision,
    fill_assumption double precision,
    required_move_pct double precision,
    buy_under double precision,
    predicted_p2x double precision,
    predicted_p5x double precision,
    ev_multiple double precision,
    tier text,
    synthetic_legs jsonb DEFAULT '[]'::jsonb NOT NULL,
    structure text DEFAULT 'long_option'::text NOT NULL,
    entry_price double precision,
    exit_cost_estimate double precision,
    secured_cash double precision,
    max_profit double precision,
    max_loss double precision,
    break_even double precision,
    effective_assignment_price double precision,
    probability_profit double precision,
    probability_assignment double precision,
    probability_touch double precision,
    expected_value double precision,
    risk_adjusted_expectancy double precision,
    tail_cvar double precision,
    data_confidence double precision,
    execution_confidence double precision,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    paper_state text,
    discovery_lane text,
    thesis_id bigint,
    relative_value_id bigint,
    model_version text,
    market_regime text,
    fair_low double precision,
    fair_high double precision,
    modeled_net_edge double precision,
    route_version text,
    strategy_route jsonb DEFAULT '{}'::jsonb NOT NULL,
    market_regime_detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    event_state text,
    CONSTRAINT ck_option_decision_market_regime_detail_object CHECK ((jsonb_typeof(market_regime_detail) = 'object'::text)),
    CONSTRAINT ck_option_decision_paper_state CHECK (((paper_state IS NULL) OR (paper_state = ANY (ARRAY['COLLECTING'::text, 'WATCH'::text, 'PAPER_READY'::text, 'REJECT'::text])))),
    CONSTRAINT ck_option_decision_strategy_route_object CHECK ((jsonb_typeof(strategy_route) = 'object'::text))
);

CREATE TABLE analysis.option_discovery_candidate (
    run_id uuid NOT NULL,
    instrument_id bigint NOT NULL,
    stage text NOT NULL,
    discovery_score double precision NOT NULL,
    surface_reason text NOT NULL,
    primary_edge text NOT NULL,
    causal_exposure text NOT NULL,
    catalyst_start date,
    catalyst_end date,
    earliest_signal_at timestamp with time zone,
    timeliness text NOT NULL,
    source_root_count integer DEFAULT 0 NOT NULL,
    evidence_completeness integer DEFAULT 0 NOT NULL,
    data_readiness text NOT NULL,
    execution_ready boolean DEFAULT false NOT NULL,
    next_evidence text NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ck_option_discovery_evidence CHECK (((evidence_completeness >= 0) AND (evidence_completeness <= 5))),
    CONSTRAINT ck_option_discovery_readiness CHECK ((data_readiness = ANY (ARRAY['A'::text, 'B'::text, 'C'::text, 'D'::text]))),
    CONSTRAINT ck_option_discovery_stage CHECK ((stage = ANY (ARRAY['DISCOVERED'::text, 'UNDERWRITING'::text, 'STRUCTURED'::text, 'PUBLISHED'::text])))
);

CREATE TABLE analysis.option_discovery_run (
    run_id uuid NOT NULL,
    universe_hash text NOT NULL,
    started_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone,
    provider text,
    market_session text,
    symbols_considered integer DEFAULT 0 NOT NULL,
    symbols_with_chains integer DEFAULT 0 NOT NULL,
    contracts_evaluated integer DEFAULT 0 NOT NULL,
    manifest jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE analysis.option_event (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    instrument_id bigint NOT NULL,
    objective_version text DEFAULT 'short_horizon_convex_v1'::text NOT NULL,
    event_type text DEFAULT 'selloff'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    detected_at timestamp with time zone NOT NULL,
    started_at timestamp with time zone NOT NULL,
    reference_price double precision NOT NULL,
    event_low double precision NOT NULL,
    trigger_intraday_pct double precision,
    trigger_one_day_pct double precision,
    trigger_three_session_pct double precision,
    severity_score double precision NOT NULL,
    event_rank integer,
    material_evidence_count integer DEFAULT 0 NOT NULL,
    enrolled_at timestamp with time zone,
    last_signal_at timestamp with time zone,
    no_active_signal_sessions integer DEFAULT 0 NOT NULL,
    closed_at timestamp with time zone,
    close_reason text,
    provenance jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    cohort_id uuid NOT NULL,
    data_quality_status text DEFAULT 'valid'::text NOT NULL,
    trigger_reason text,
    quote_age_minutes double precision,
    reference_trading_date date,
    reference_source_id text,
    reference_available_at timestamp with time zone,
    invalidated_at timestamp with time zone,
    invalidation_reason text,
    priority_components jsonb DEFAULT '{}'::jsonb NOT NULL,
    capacity_defer_reason text,
    CONSTRAINT ck_option_event_data_quality_status CHECK ((data_quality_status = ANY (ARRAY['valid'::text, 'invalid_reference_bar'::text, 'stale_quote'::text, 'missing_reference'::text, 'lookahead_blocked'::text, 'provider_unconfirmed'::text]))),
    CONSTRAINT ck_option_event_low CHECK (((event_low > (0)::double precision) AND (reference_price > (0)::double precision))),
    CONSTRAINT ck_option_event_status CHECK ((status = ANY (ARRAY['active'::text, 'deferred_capacity'::text, 'closed'::text, 'invalidated'::text]))),
    CONSTRAINT ck_option_event_type CHECK ((event_type = 'selloff'::text))
);

CREATE TABLE analysis.option_event_agent_batch (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_id uuid NOT NULL,
    capture_id uuid,
    trigger text NOT NULL,
    fingerprint_key text NOT NULL,
    fingerprint jsonb NOT NULL,
    provider text DEFAULT 'codex'::text NOT NULL,
    model text NOT NULL,
    reasoning_effort text NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    task_count integer DEFAULT 0 NOT NULL,
    agent_run_id uuid,
    telemetry jsonb DEFAULT '{}'::jsonb NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    cohort_id uuid NOT NULL,
    experiment_id uuid,
    arm text,
    paired_task_id uuid,
    evidence_fingerprint text,
    prompt_version text,
    schema_version text,
    baseline_version text,
    validation_status text,
    validation_detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    latency_ms integer,
    CONSTRAINT ck_option_event_agent_batch_status CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'completed'::text, 'failed'::text, 'skipped'::text]))),
    CONSTRAINT ck_option_event_agent_batch_task_count CHECK (((task_count >= 0) AND (task_count <= 12))),
    CONSTRAINT ck_option_event_agent_batch_trigger CHECK ((trigger = ANY (ARRAY['event_established'::text, 'underlying_move_2pct'::text, 'material_iv_change'::text, 'new_material_evidence'::text, 'signal_family_transition'::text, 'preopen_review'::text])))
);

CREATE TABLE analysis.option_event_capture (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_id uuid NOT NULL,
    snapshot_id bigint,
    capture_generation_id bigint,
    scheduled_at timestamp with time zone NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    status text NOT NULL,
    expected_contract_count integer DEFAULT 0 NOT NULL,
    received_contract_count integer DEFAULT 0 NOT NULL,
    completeness double precision,
    continuity_pct double precision,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    canonical_continuity_pct double precision,
    original_continuity_pct double precision,
    CONSTRAINT ck_option_event_capture_status CHECK ((status = ANY (ARRAY['complete'::text, 'partial'::text, 'failed'::text, 'deferred'::text])))
);

CREATE TABLE analysis.option_event_contract (
    id bigint NOT NULL,
    event_id uuid NOT NULL,
    contract_id bigint,
    contract_key text NOT NULL,
    option_type text NOT NULL,
    expiration date NOT NULL,
    target_delta double precision NOT NULL,
    is_initial boolean DEFAULT false NOT NULL,
    replaces_contract_id bigint,
    initial_capture_generation_id bigint,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    retired_at timestamp with time zone,
    reason text,
    ladder_slot_key text NOT NULL,
    retired_reason text,
    CONSTRAINT ck_option_event_contract_type CHECK ((option_type = ANY (ARRAY['call'::text, 'put'::text])))
);

ALTER TABLE analysis.option_event_contract ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME analysis.option_event_contract_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE analysis.option_event_detector_run (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    cohort_id uuid NOT NULL,
    scheduled_at timestamp with time zone NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    expected_symbols integer DEFAULT 0 NOT NULL,
    received_symbols integer DEFAULT 0 NOT NULL,
    fresh_symbols integer DEFAULT 0 NOT NULL,
    quote_age_p95_minutes double precision,
    provider_run_id uuid,
    status text NOT NULL,
    failure_reasons jsonb DEFAULT '[]'::jsonb NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_option_event_detector_run_counts CHECK (((expected_symbols >= 0) AND (received_symbols >= 0) AND (fresh_symbols >= 0))),
    CONSTRAINT ck_option_event_detector_run_status CHECK ((status = ANY (ARRAY['succeeded'::text, 'failed'::text, 'skipped'::text])))
);

CREATE TABLE analysis.option_event_signal (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_id uuid NOT NULL,
    event_contract_id bigint,
    capture_id uuid,
    decision_id uuid,
    snapshot_id bigint,
    contract_id bigint NOT NULL,
    strategy_key text NOT NULL,
    strategy_revision_id bigint,
    objective_version text DEFAULT 'short_horizon_convex_v1'::text NOT NULL,
    status text DEFAULT 'shadow'::text NOT NULL,
    signal_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    selection_score double precision,
    lower_confidence_expectancy double precision,
    maximum_loss numeric(20,6),
    gate_result jsonb DEFAULT '{}'::jsonb NOT NULL,
    ticket jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    cohort_id uuid NOT NULL,
    CONSTRAINT ck_option_event_signal_availability CHECK ((signal_at >= available_at)),
    CONSTRAINT ck_option_event_signal_status CHECK ((status = ANY (ARRAY['shadow'::text, 'ticketed'::text, 'risk_blocked'::text, 'stale'::text, 'unfilled'::text, 'entered'::text, 'partial_exited'::text, 'exited'::text, 'invalidated'::text, 'unmeasurable'::text, 'rejected'::text])))
);

CREATE TABLE analysis.option_event_spot (
    event_id uuid NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    price double precision NOT NULL,
    source_id text,
    one_day_pct double precision,
    three_session_pct double precision
);

CREATE TABLE analysis.option_feature (
    id bigint NOT NULL,
    run_id uuid NOT NULL,
    snapshot_id bigint NOT NULL,
    contract_id bigint NOT NULL,
    quote_observed_at timestamp with time zone NOT NULL,
    feature_version text NOT NULL,
    modeled_iv double precision,
    modeled_delta double precision,
    modeled_gamma double precision,
    modeled_theta double precision,
    modeled_vega double precision,
    dte integer,
    spread_pct double precision,
    iv_rank double precision,
    iv_percentile double precision,
    liquidity_score double precision,
    flow_score double precision,
    convexity_score double precision,
    required_2x_price double precision,
    required_5x_price double precision,
    required_10x_price double precision,
    required_move_pct double precision,
    ev_inputs jsonb DEFAULT '{}'::jsonb NOT NULL,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE analysis.option_feature ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME analysis.option_feature_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE analysis.option_gate_result (
    run_id uuid NOT NULL,
    instrument_id bigint NOT NULL,
    gate_code text NOT NULL,
    passed boolean NOT NULL,
    reason text NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE analysis.option_history_anomaly (
    id bigint NOT NULL,
    snapshot_id bigint NOT NULL,
    contract_id bigint,
    expiration date,
    option_type text,
    anomaly_type text NOT NULL,
    state text NOT NULL,
    observed_value double precision,
    expected_value double precision,
    z_score double precision,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    feature_version text DEFAULT 'history-v2'::text NOT NULL,
    CONSTRAINT option_history_anomaly_option_type_check CHECK ((option_type = ANY (ARRAY['call'::text, 'put'::text]))),
    CONSTRAINT option_history_anomaly_state_check CHECK ((state = ANY (ARRAY['active'::text, 'collecting'::text])))
);

ALTER TABLE analysis.option_history_anomaly ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME analysis.option_history_anomaly_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE analysis.option_history_canary (
    id bigint NOT NULL,
    model_revision text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE analysis.option_history_canary ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME analysis.option_history_canary_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE analysis.option_liquidity_sla (
    sla_id text NOT NULL,
    as_of timestamp with time zone NOT NULL,
    source_id text NOT NULL,
    ingest_run_id uuid NOT NULL,
    payload_hash text NOT NULL,
    parent_snapshot_id text,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    payload_id bigint NOT NULL
);

CREATE TABLE analysis.option_opportunity_observation (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_id uuid NOT NULL,
    capture_id uuid,
    capture_generation_id bigint,
    capture_generation_key text NOT NULL,
    event_contract_id bigint,
    contract_id bigint NOT NULL,
    strategy_key text NOT NULL,
    strategy_revision_id bigint,
    objective_version text DEFAULT 'short_horizon_convex_v1'::text NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    expiration date NOT NULL,
    quote jsonb DEFAULT '{}'::jsonb NOT NULL,
    liquid boolean NOT NULL,
    data_status text DEFAULT 'ok'::text NOT NULL,
    selection_stage text DEFAULT 'observed'::text NOT NULL,
    miss_reason text,
    signal_id uuid,
    paper_order_id uuid,
    selection_score double precision,
    lower_confidence_expectancy double precision,
    entry_fill_at timestamp with time zone,
    entry_fill_price numeric(20,6),
    return_1_session double precision,
    return_3_session double precision,
    return_5_session double precision,
    return_10_session double precision,
    time_to_2x_sessions integer,
    time_to_3x_sessions integer,
    time_to_4x_sessions integer,
    executable_peak_return double precision,
    realized_return double precision,
    mae double precision,
    giveback double precision,
    exit_efficiency double precision,
    exit_fill_at timestamp with time zone,
    exit_fill_price numeric(20,6),
    outcome_classification text DEFAULT 'observing'::text NOT NULL,
    measured_through timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    cohort_id uuid NOT NULL,
    lane text DEFAULT 'recovery'::text NOT NULL,
    episode_key text NOT NULL,
    sample_eligible boolean DEFAULT false NOT NULL,
    quarantine_reason text,
    calibration_cohort text,
    CONSTRAINT ck_option_opportunity_availability CHECK ((available_at IS NOT NULL)),
    CONSTRAINT ck_option_opportunity_classification CHECK ((outcome_classification = ANY (ARRAY['observing'::text, 'captured'::text, 'missed'::text, 'unfilled'::text, 'unmeasurable'::text]))),
    CONSTRAINT ck_option_opportunity_data_status CHECK ((data_status = ANY (ARRAY['ok'::text, 'stale_quote'::text, 'continuity_missing'::text, 'lookahead_blocked'::text, 'invalid_event_reference'::text]))),
    CONSTRAINT ck_option_opportunity_miss_reason CHECK (((miss_reason IS NULL) OR (miss_reason = ANY (ARRAY['not_featured'::text, 'gate_reject'::text, 'ranked_out'::text, 'not_published'::text, 'unfilled'::text, 'risk_blocked'::text, 'captured'::text, 'unmeasurable'::text])))),
    CONSTRAINT ck_option_opportunity_selection_stage CHECK ((selection_stage = ANY (ARRAY['observed'::text, 'eligible'::text, 'ranked_out'::text, 'published'::text, 'ticketed'::text, 'filled'::text, 'exited'::text])))
);

CREATE TABLE analysis.option_outcome (
    decision_id uuid NOT NULL,
    maturity_state text NOT NULL,
    observed_through timestamp with time zone,
    return_1d double precision,
    return_5d double precision,
    return_20d double precision,
    return_60d double precision,
    peak_return double precision,
    max_drawdown double precision,
    time_to_2x_days integer,
    time_to_5x_days integer,
    time_to_10x_days integer,
    realized_exit_return double precision,
    realized_exit_basis text,
    stock_move_effect double precision,
    iv_effect double precision,
    theta_effect double precision,
    spread_effect double precision,
    unexplained_effect double precision,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    current_return double precision,
    paper_status text,
    credit_captured double precision,
    collateral_return double precision,
    assigned_basis double precision,
    strike_touched boolean,
    assignment_return_1d double precision,
    assignment_return_5d double precision,
    assignment_return_20d double precision,
    assignment_return_60d double precision,
    outcome_source text DEFAULT 'generic'::text NOT NULL,
    shadow_trade_id uuid,
    objective_version text DEFAULT 'legacy'::text NOT NULL,
    outcome_classification text DEFAULT 'legacy_non_executable'::text NOT NULL,
    promotion_eligible boolean DEFAULT false NOT NULL,
    entry_fill_at timestamp with time zone,
    entry_fill_price numeric(20,6),
    exit_fill_at timestamp with time zone,
    exit_fill_price numeric(20,6),
    exit_reason text,
    fee_total numeric(20,6),
    slippage_total numeric(20,6),
    return_3d double precision,
    return_10d double precision,
    time_to_3x_days integer,
    time_to_4x_days integer,
    executable_peak_return double precision,
    mae double precision,
    giveback double precision,
    exit_efficiency double precision,
    lane text,
    episode_key text,
    sample_eligible boolean DEFAULT false NOT NULL,
    quarantine_reason text,
    calibration_cohort text,
    CONSTRAINT ck_option_outcome_recovery_classification CHECK ((outcome_classification = ANY (ARRAY['legacy_non_executable'::text, 'captured'::text, 'missed'::text, 'unfilled'::text, 'unmeasurable'::text, 'observing'::text]))),
    CONSTRAINT ck_option_outcome_source CHECK ((outcome_source = ANY (ARRAY['generic'::text, 'options_history_v3'::text]))),
    CONSTRAINT ck_option_outcome_v3_shadow CHECK (((outcome_source <> 'options_history_v3'::text) OR (shadow_trade_id IS NOT NULL)))
);

CREATE TABLE analysis.option_recovery_cohort (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    objective_version text NOT NULL,
    code_version text NOT NULL,
    started_at timestamp with time zone NOT NULL,
    status text DEFAULT 'collecting'::text NOT NULL,
    required_qualified_dates integer DEFAULT 5 NOT NULL,
    qualified_at timestamp with time zone,
    blockers jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_option_recovery_cohort_required_dates CHECK ((required_qualified_dates > 0)),
    CONSTRAINT ck_option_recovery_cohort_status CHECK ((status = ANY (ARRAY['collecting'::text, 'qualified'::text, 'retired'::text])))
);

CREATE TABLE analysis.option_recovery_event_session_quality (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    cohort_id uuid NOT NULL,
    event_id uuid NOT NULL,
    trading_date date NOT NULL,
    scheduled_slots integer DEFAULT 0 NOT NULL,
    usable_slots integer DEFAULT 0 NOT NULL,
    complete_slots integer DEFAULT 0 NOT NULL,
    contract_completeness double precision,
    canonical_continuity double precision,
    original_continuity double precision,
    capture_p95_latency_minutes double precision,
    data_defects jsonb DEFAULT '[]'::jsonb NOT NULL,
    qualification_result boolean DEFAULT false CONSTRAINT option_recovery_event_session_qua_qualification_result_not_null NOT NULL,
    qualification_reasons jsonb DEFAULT '[]'::jsonb CONSTRAINT option_recovery_event_session_qu_qualification_reasons_not_null NOT NULL,
    computed_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE analysis.option_recovery_program_session (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    cohort_id uuid NOT NULL,
    trading_date date NOT NULL,
    active_event_count integer DEFAULT 0 NOT NULL,
    detector_scheduled_runs integer DEFAULT 0 CONSTRAINT option_recovery_program_sessio_detector_scheduled_runs_not_null NOT NULL,
    detector_succeeded_runs integer DEFAULT 0 CONSTRAINT option_recovery_program_sessio_detector_succeeded_runs_not_null NOT NULL,
    provider_expected_symbols integer DEFAULT 0 CONSTRAINT option_recovery_program_sess_provider_expected_symbols_not_null NOT NULL,
    provider_received_symbols integer DEFAULT 0 CONSTRAINT option_recovery_program_sess_provider_received_symbols_not_null NOT NULL,
    fresh_event_trigger_quotes integer DEFAULT 0 CONSTRAINT option_recovery_program_ses_fresh_event_trigger_quotes_not_null NOT NULL,
    quote_age_p95_minutes double precision,
    event_scheduled_slots integer DEFAULT 0 NOT NULL,
    event_usable_slots integer DEFAULT 0 NOT NULL,
    contract_completeness double precision,
    canonical_continuity double precision,
    original_continuity double precision,
    capture_p95_latency_minutes double precision,
    critical_defects jsonb DEFAULT '[]'::jsonb NOT NULL,
    qualification_result boolean DEFAULT false NOT NULL,
    qualification_reasons jsonb DEFAULT '[]'::jsonb NOT NULL,
    policy_version text NOT NULL,
    computed_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE analysis.option_relative_value (
    id bigint NOT NULL,
    analysis_run_id uuid NOT NULL,
    capture_generation_id bigint NOT NULL,
    contract_id bigint NOT NULL,
    model_revision text NOT NULL,
    classification text NOT NULL,
    fair_low double precision,
    fair_high double precision,
    modeled_net_edge double precision,
    edge_side text,
    confidence double precision,
    quality_status text NOT NULL,
    blockers text[] DEFAULT '{}'::text[] NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT option_relative_value_classification_check CHECK ((classification = ANY (ARRAY['relative_cheap'::text, 'relative_rich'::text, 'historical_static_arbitrage_candidate'::text, 'verified_static_arbitrage_candidate'::text, 'rejected'::text])))
);

ALTER TABLE analysis.option_relative_value ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME analysis.option_relative_value_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE analysis.option_relative_value_verification (
    id bigint NOT NULL,
    relative_value_id bigint NOT NULL,
    verified_at timestamp with time zone DEFAULT now() NOT NULL,
    status text NOT NULL,
    blockers text[] DEFAULT '{}'::text[] NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT option_relative_value_verification_status_check CHECK ((status = ANY (ARRAY['verified'::text, 'rejected'::text, 'unavailable'::text])))
);

ALTER TABLE analysis.option_relative_value_verification ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME analysis.option_relative_value_verification_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE analysis.option_surface_shift (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    instrument_id bigint NOT NULL,
    current_capture_generation_id bigint NOT NULL,
    previous_capture_generation_id bigint NOT NULL,
    current_analysis_run_id uuid NOT NULL,
    previous_analysis_run_id uuid NOT NULL,
    as_of timestamp with time zone NOT NULL,
    previous_as_of timestamp with time zone,
    feature_version text NOT NULL,
    tenors integer[] DEFAULT ARRAY[7, 14, 30, 60, 90] NOT NULL,
    w1_shift double precision,
    tail_mass_change double precision,
    skew_shift double precision,
    term_shift double precision,
    evidence_state text NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ck_surface_shift_evidence_state CHECK ((evidence_state = ANY (ARRAY['ready'::text, 'insufficient_surface_evidence'::text, 'unavailable'::text])))
);

CREATE TABLE analysis.option_surface_summary (
    id bigint NOT NULL,
    snapshot_id bigint NOT NULL,
    expiration date NOT NULL,
    option_type text NOT NULL,
    feature_version text NOT NULL,
    dte integer NOT NULL,
    atm_iv double precision,
    delta_25_iv double precision,
    skew_25 double precision,
    smile_slope double precision,
    smile_curvature double precision,
    term_slope double precision,
    average_spread_pct double precision,
    liquidity_score double precision,
    atm_iv_change double precision,
    skew_25_change double precision,
    term_slope_change double precision,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    analysis_run_id uuid,
    capture_generation_id bigint,
    fit_method text,
    fit_status text,
    eligible_point_count integer,
    group_duration_seconds double precision,
    max_quote_age_seconds double precision,
    fit_rmse double precision,
    candidate_count integer DEFAULT 0 NOT NULL,
    CONSTRAINT option_surface_summary_option_type_check CHECK ((option_type = ANY (ARRAY['call'::text, 'put'::text])))
);

ALTER TABLE analysis.option_surface_summary ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME analysis.option_surface_summary_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE analysis.phase4_allocation_signing_secret (
    singleton boolean DEFAULT true NOT NULL,
    secret bytea NOT NULL,
    installed_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT phase4_allocation_signing_secret_secret_check CHECK ((length(secret) >= 16)),
    CONSTRAINT phase4_allocation_signing_secret_singleton_check CHECK (singleton)
);

CREATE TABLE analysis.portfolio_allocation_item (
    allocation_item_id text NOT NULL,
    allocation_id text NOT NULL,
    candidate_id text DEFAULT ''::text NOT NULL,
    ticker text NOT NULL,
    strategy_forecast_id text,
    action_id text,
    rank_id text,
    hypothesis_id uuid,
    disposition text NOT NULL,
    target_weight double precision NOT NULL,
    current_weight double precision DEFAULT 0 NOT NULL,
    marginal_book_utility double precision NOT NULL,
    trace jsonb NOT NULL,
    blockers jsonb DEFAULT '[]'::jsonb NOT NULL,
    funding_source text,
    funding_amount double precision,
    input_hash character(64) NOT NULL,
    content_hash character(64) NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    funding_sources jsonb DEFAULT '{}'::jsonb,
    CONSTRAINT phase4_allocation_item_funding_amount_shape CHECK (((funding_amount IS NULL) OR ((funding_amount < 'Infinity'::double precision) AND (funding_amount > '-Infinity'::double precision) AND (funding_amount > (0)::double precision)))),
    CONSTRAINT phase4_allocation_item_funding_sources_shape CHECK ((jsonb_typeof(funding_sources) = 'object'::text)),
    CONSTRAINT portfolio_allocation_item_blockers_check CHECK ((jsonb_typeof(blockers) = 'array'::text)),
    CONSTRAINT portfolio_allocation_item_check CHECK (((ticker = 'CASH'::text) OR (candidate_id <> ''::text))),
    CONSTRAINT portfolio_allocation_item_check1 CHECK (((input_hash ~ '^[0-9a-f]{64}$'::text) AND ((input_hash)::text <> repeat('0'::text, 64)) AND (allocation_item_id = ('allocation-item:'::text || (input_hash)::text)))),
    CONSTRAINT portfolio_allocation_item_check2 CHECK (((ticker = 'CASH'::text) OR (disposition <> 'selected'::text) OR ((strategy_forecast_id IS NOT NULL) AND (action_id IS NOT NULL)))),
    CONSTRAINT portfolio_allocation_item_check3 CHECK (((ticker = 'CASH'::text) OR (disposition <> 'selected'::text) OR (rank_id IS NOT NULL))),
    CONSTRAINT portfolio_allocation_item_check4 CHECK (((disposition <> 'selected'::text) OR ((ticker = 'CASH'::text) AND (target_weight > (0)::double precision) AND (marginal_book_utility >= (0)::double precision)) OR ((target_weight > (0)::double precision) AND (marginal_book_utility > (0)::double precision)))),
    CONSTRAINT portfolio_allocation_item_check6 CHECK (((ticker = 'CASH'::text) OR (disposition <> 'selected'::text) OR ((funding_amount IS NOT NULL) AND (funding_amount > (0)::double precision)))),
    CONSTRAINT portfolio_allocation_item_content_hash_check CHECK (((content_hash ~ '^[0-9a-f]{64}$'::text) AND ((content_hash)::text <> repeat('0'::text, 64)))),
    CONSTRAINT portfolio_allocation_item_current_weight_check CHECK (((current_weight < 'Infinity'::double precision) AND (current_weight > '-Infinity'::double precision) AND (current_weight >= (0)::double precision) AND (current_weight <= (1)::double precision))),
    CONSTRAINT portfolio_allocation_item_disposition_check CHECK ((disposition = ANY (ARRAY['selected'::text, 'ranked_out'::text, 'rejected'::text, 'rollback'::text]))),
    CONSTRAINT portfolio_allocation_item_marginal_book_utility_check CHECK (((marginal_book_utility < 'Infinity'::double precision) AND (marginal_book_utility > '-Infinity'::double precision))),
    CONSTRAINT portfolio_allocation_item_target_weight_check CHECK (((target_weight < 'Infinity'::double precision) AND (target_weight > '-Infinity'::double precision) AND (target_weight >= (0)::double precision) AND (target_weight <= (1)::double precision))),
    CONSTRAINT portfolio_allocation_item_ticker_check CHECK ((ticker <> ''::text)),
    CONSTRAINT portfolio_allocation_item_trace_check CHECK ((jsonb_typeof(trace) = 'object'::text))
);

CREATE TABLE analysis.portfolio_allocation_snapshot (
    allocation_id text NOT NULL,
    as_of timestamp with time zone NOT NULL,
    input_cutoff timestamp with time zone NOT NULL,
    status text NOT NULL,
    cash_hurdle double precision,
    forecast_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    action_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    strategy_registry_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    input_hash character(64) NOT NULL,
    content_hash character(64) NOT NULL,
    available_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT portfolio_allocation_snapshot_action_ids_check CHECK ((jsonb_typeof(action_ids) = 'array'::text)),
    CONSTRAINT portfolio_allocation_snapshot_cash_hurdle_check CHECK (((cash_hurdle IS NULL) OR ((cash_hurdle < 'Infinity'::double precision) AND (cash_hurdle > '-Infinity'::double precision) AND (cash_hurdle >= (0)::double precision)))),
    CONSTRAINT portfolio_allocation_snapshot_check CHECK ((as_of = input_cutoff)),
    CONSTRAINT portfolio_allocation_snapshot_check1 CHECK (((status <> 'available'::text) OR (cash_hurdle > (0)::double precision))),
    CONSTRAINT portfolio_allocation_snapshot_check2 CHECK ((allocation_id = ('allocation:'::text || (input_hash)::text))),
    CONSTRAINT portfolio_allocation_snapshot_content_hash_check CHECK (((content_hash ~ '^[0-9a-f]{64}$'::text) AND ((content_hash)::text <> repeat('0'::text, 64)))),
    CONSTRAINT portfolio_allocation_snapshot_forecast_ids_check CHECK ((jsonb_typeof(forecast_ids) = 'array'::text)),
    CONSTRAINT portfolio_allocation_snapshot_input_hash_check CHECK (((input_hash ~ '^[0-9a-f]{64}$'::text) AND ((input_hash)::text <> repeat('0'::text, 64)))),
    CONSTRAINT portfolio_allocation_snapshot_status_check CHECK ((status = ANY (ARRAY['available'::text, 'cash_only'::text, 'unavailable'::text]))),
    CONSTRAINT portfolio_allocation_snapshot_strategy_registry_ids_check CHECK ((jsonb_typeof(strategy_registry_ids) = 'array'::text))
);

CREATE TABLE analysis.portfolio_drift_evidence (
    decision_id text NOT NULL,
    allocation_id text NOT NULL,
    allocation_item_id text NOT NULL,
    drift_score double precision NOT NULL,
    rollback_threshold double precision NOT NULL,
    proposed_weight double precision NOT NULL,
    action text NOT NULL,
    input_cutoff timestamp with time zone NOT NULL,
    input_hash character(64) NOT NULL,
    content_hash character(64) NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT portfolio_drift_evidence_action_check CHECK ((action = ANY (ARRAY['hold'::text, 'reduce'::text, 'rollback'::text, 'unavailable'::text]))),
    CONSTRAINT portfolio_drift_evidence_check CHECK (((action <> 'reduce'::text) OR ((drift_score < rollback_threshold) AND (drift_score >= (rollback_threshold / (2)::double precision))))),
    CONSTRAINT portfolio_drift_evidence_check1 CHECK (((action <> 'rollback'::text) OR (drift_score >= rollback_threshold))),
    CONSTRAINT portfolio_drift_evidence_check2 CHECK (((action <> 'hold'::text) OR (drift_score < (rollback_threshold / (2)::double precision)))),
    CONSTRAINT portfolio_drift_evidence_check3 CHECK ((decision_id = ('drift:'::text || (input_hash)::text))),
    CONSTRAINT portfolio_drift_evidence_content_hash_check CHECK ((content_hash ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT portfolio_drift_evidence_drift_score_check CHECK (((drift_score < 'Infinity'::double precision) AND (drift_score > '-Infinity'::double precision) AND (drift_score >= (0)::double precision))),
    CONSTRAINT portfolio_drift_evidence_input_hash_check CHECK ((input_hash ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT portfolio_drift_evidence_metadata_check CHECK ((jsonb_typeof(metadata) = 'object'::text)),
    CONSTRAINT portfolio_drift_evidence_proposed_weight_check CHECK (((proposed_weight < 'Infinity'::double precision) AND (proposed_weight >= (0)::double precision) AND (proposed_weight <= (1)::double precision))),
    CONSTRAINT portfolio_drift_evidence_rollback_threshold_check CHECK (((rollback_threshold < 'Infinity'::double precision) AND (rollback_threshold > (0)::double precision)))
);

CREATE TABLE analysis.probabilistic_portfolio_scenario_artifact (
    scenario_artifact_id text CONSTRAINT probabilistic_portfolio_scenario__scenario_artifact_id_not_null NOT NULL,
    allocation_id text CONSTRAINT probabilistic_portfolio_scenario_artifac_allocation_id_not_null NOT NULL,
    model_version text CONSTRAINT probabilistic_portfolio_scenario_artifac_model_version_not_null NOT NULL,
    probability_semantics text CONSTRAINT probabilistic_portfolio_scenario_probability_semantics_not_null NOT NULL,
    scenarios jsonb NOT NULL,
    tail_dependence jsonb CONSTRAINT probabilistic_portfolio_scenario_artif_tail_dependence_not_null NOT NULL,
    simultaneous_unwind jsonb CONSTRAINT probabilistic_portfolio_scenario_a_simultaneous_unwind_not_null NOT NULL,
    input_cutoff timestamp with time zone NOT NULL,
    input_hash character(64) NOT NULL,
    content_hash character(64) NOT NULL,
    available_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT probabilistic_portfolio_scenario_arti_simultaneous_unwind_check CHECK (((jsonb_typeof(simultaneous_unwind) = 'object'::text) AND (simultaneous_unwind <> '{}'::jsonb))),
    CONSTRAINT probabilistic_portfolio_scenario_artifact_check CHECK ((scenario_artifact_id = ('scenario:'::text || (input_hash)::text))),
    CONSTRAINT probabilistic_portfolio_scenario_artifact_content_hash_check CHECK (((content_hash ~ '^[0-9a-f]{64}$'::text) AND ((content_hash)::text <> repeat('0'::text, 64)))),
    CONSTRAINT probabilistic_portfolio_scenario_artifact_input_hash_check CHECK (((input_hash ~ '^[0-9a-f]{64}$'::text) AND ((input_hash)::text <> repeat('0'::text, 64)))),
    CONSTRAINT probabilistic_portfolio_scenario_artifact_scenarios_check CHECK (((jsonb_typeof(scenarios) = 'array'::text) AND (jsonb_array_length(scenarios) > 0))),
    CONSTRAINT probabilistic_portfolio_scenario_artifact_tail_dependence_check CHECK (((jsonb_typeof(tail_dependence) = 'object'::text) AND (tail_dependence <> '{}'::jsonb)))
);

CREATE TABLE analysis.reject_summary (
    id bigint NOT NULL,
    run_id uuid NOT NULL,
    strategy_revision_id bigint,
    instrument_id bigint,
    gate_code text NOT NULL,
    reject_count integer NOT NULL,
    sampled_decision_keys text[] DEFAULT '{}'::text[] NOT NULL,
    CONSTRAINT reject_summary_reject_count_check CHECK ((reject_count >= 0))
);

ALTER TABLE analysis.reject_summary ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME analysis.reject_summary_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE analysis.research_evaluator_output (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    research_trial_id uuid NOT NULL,
    trial_result_id uuid NOT NULL,
    analysis_run_id uuid NOT NULL,
    evidence_kind text NOT NULL,
    evaluator_id text NOT NULL,
    evaluator_code_version text NOT NULL,
    input_hash character(64) NOT NULL,
    universe_hash character(64) NOT NULL,
    feature_hash character(64) NOT NULL,
    sample_count integer NOT NULL,
    domain_valid boolean NOT NULL,
    raw_output jsonb NOT NULL,
    output_hash character(64) DEFAULT repeat('0'::text, 64) NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    available_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    signature text DEFAULT ''::text NOT NULL,
    CONSTRAINT research_evaluator_output_check CHECK ((available_at >= created_at)),
    CONSTRAINT research_evaluator_output_evidence_kind_check CHECK ((evidence_kind = ANY (ARRAY['controls'::text, 'cpcv_paths'::text, 'neutralization'::text, 'parameter_stability'::text, 'mechanism_falsification'::text, 'multiple_testing'::text]))),
    CONSTRAINT research_evaluator_output_feature_hash_check CHECK (((feature_hash ~ '^[0-9a-fA-F]{64}$'::text) AND (lower((feature_hash)::text) <> repeat('0'::text, 64)))),
    CONSTRAINT research_evaluator_output_input_hash_check CHECK (((input_hash ~ '^[0-9a-fA-F]{64}$'::text) AND (lower((input_hash)::text) <> repeat('0'::text, 64)))),
    CONSTRAINT research_evaluator_output_raw_output_check CHECK ((jsonb_typeof(raw_output) = 'object'::text)),
    CONSTRAINT research_evaluator_output_sample_count_check CHECK ((sample_count > 0)),
    CONSTRAINT research_evaluator_output_universe_hash_check CHECK (((universe_hash ~ '^[0-9a-fA-F]{64}$'::text) AND (lower((universe_hash)::text) <> repeat('0'::text, 64))))
);

ALTER TABLE analysis.research_evaluator_output OWNER TO market_research_signer;

CREATE TABLE analysis.research_evaluator_signing_secret (
    singleton boolean DEFAULT true NOT NULL,
    secret bytea NOT NULL,
    installed_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT research_evaluator_signing_secret_secret_check CHECK ((length(secret) >= 16)),
    CONSTRAINT research_evaluator_signing_secret_singleton_check CHECK (singleton)
);

ALTER TABLE analysis.research_evaluator_signing_secret OWNER TO market_research_signer;

CREATE TABLE analysis.research_evidence_manifest (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    research_trial_id uuid NOT NULL,
    trial_result_id uuid NOT NULL,
    evidence_kind text NOT NULL,
    evaluator_id text NOT NULL,
    sample_count integer NOT NULL,
    domain_valid boolean NOT NULL,
    payload jsonb NOT NULL,
    evidence_hash character(64) DEFAULT repeat('0'::text, 64) NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    available_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    evaluator_output_id uuid,
    evaluator_code_version text,
    input_hash character(64),
    universe_hash character(64),
    feature_hash character(64),
    CONSTRAINT research_evidence_manifest_check CHECK ((available_at >= created_at)),
    CONSTRAINT research_evidence_manifest_evidence_kind_check CHECK ((evidence_kind = ANY (ARRAY['controls'::text, 'cpcv_paths'::text, 'neutralization'::text, 'parameter_stability'::text, 'mechanism_falsification'::text, 'multiple_testing'::text]))),
    CONSTRAINT research_evidence_manifest_payload_check CHECK ((jsonb_typeof(payload) = 'object'::text)),
    CONSTRAINT research_evidence_manifest_sample_count_check CHECK ((sample_count > 0))
);

CREATE TABLE analysis.research_trial (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    experiment_family_id uuid NOT NULL,
    trial_key text NOT NULL,
    input_cutoff timestamp with time zone NOT NULL,
    code_version text NOT NULL,
    input_hash character(64) NOT NULL,
    parameters jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    failure_reason text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    outcome jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT research_trial_check CHECK (((finished_at IS NULL) OR (finished_at >= started_at))),
    CONSTRAINT research_trial_status_check CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'failed'::text, 'rejected'::text])))
);

CREATE TABLE analysis.run (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    run_type text NOT NULL,
    input_cutoff timestamp with time zone NOT NULL,
    code_version text NOT NULL,
    feature_versions jsonb DEFAULT '{}'::jsonb NOT NULL,
    strategy_revision_id bigint,
    input_hash character(64) NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    status text NOT NULL,
    summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    inputs jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT run_status_check CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'partial'::text, 'failed'::text])))
);

CREATE TABLE analysis.shadow_trade (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    decision_id uuid NOT NULL,
    entry_at timestamp with time zone,
    entry_price numeric(20,6),
    exit_at timestamp with time zone,
    exit_price numeric(20,6),
    status text NOT NULL,
    path jsonb DEFAULT '[]'::jsonb NOT NULL,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    pending_entry_reason text,
    entry_cohort_id bigint,
    structure text,
    market_regime text,
    fill_basis text,
    source_kind text DEFAULT 'system'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE analysis.source_signal (
    id bigint NOT NULL,
    run_id uuid NOT NULL,
    content_item_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    signal_type text NOT NULL,
    sentiment text,
    direction text,
    confidence double precision,
    thesis text,
    antithesis text,
    invalidation text,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    event_at timestamp with time zone,
    published_at timestamp with time zone,
    available_at timestamp with time zone,
    received_at timestamp with time zone,
    revision text,
    license text,
    evidence_state text,
    transformation text
);

ALTER TABLE analysis.source_signal ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME analysis.source_signal_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE analysis.strategy_comparison (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    champion_revision_id bigint NOT NULL,
    challenger_revision_id bigint NOT NULL,
    champion_trial_id uuid NOT NULL,
    challenger_trial_id uuid NOT NULL,
    champion_result_id uuid NOT NULL,
    challenger_result_id uuid NOT NULL,
    champion_result_hash character(64) NOT NULL,
    challenger_result_hash character(64) NOT NULL,
    champion_manifest_hash character(64) NOT NULL,
    challenger_manifest_hash character(64) NOT NULL,
    input_cutoff timestamp with time zone NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    input_hash character(64) NOT NULL,
    distinctness text NOT NULL,
    explanation text NOT NULL,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT strategy_comparison_check CHECK ((champion_revision_id <> challenger_revision_id)),
    CONSTRAINT strategy_comparison_check1 CHECK (((available_at <= observed_at) AND (available_at <= input_cutoff))),
    CONSTRAINT strategy_comparison_distinctness_check CHECK ((distinctness = ANY (ARRAY['distinct'::text, 'replica'::text, 'exposure_sleeve'::text, 'inconclusive'::text, 'blocked'::text]))),
    CONSTRAINT strategy_comparison_input_hash_check CHECK ((input_hash ~ '^[0-9a-f]{64}$'::text))
);

CREATE TABLE analysis.strategy_evaluation (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    strategy_revision_id bigint NOT NULL,
    evaluation_type text NOT NULL,
    evaluated_at timestamp with time zone NOT NULL,
    period_start timestamp with time zone,
    period_end timestamp with time zone,
    verdict text,
    metrics jsonb NOT NULL,
    evidence jsonb DEFAULT '[]'::jsonb NOT NULL,
    available_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    hypothesis_id uuid,
    experiment_family_id uuid,
    research_trial_id uuid,
    validation_dossier_id uuid,
    artifact_id text,
    artifact_hash character(64),
    input_hash character(64),
    lineage jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE analysis.strategy_forecast (
    id text NOT NULL,
    strategy_revision_id bigint NOT NULL,
    strategy_evaluation_id uuid,
    instrument_id bigint NOT NULL,
    opportunity_episode_id text NOT NULL,
    target text NOT NULL,
    horizon text NOT NULL,
    forecast_value double precision,
    forecast_range jsonb,
    forecast_distribution jsonb,
    probability_semantics text,
    model_artifact_id text NOT NULL,
    artifact_hash character(64) NOT NULL,
    input_hash character(64) NOT NULL,
    as_of timestamp with time zone NOT NULL,
    input_cutoff timestamp with time zone NOT NULL,
    generated_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    status text DEFAULT 'available'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    research_trial_id uuid,
    trial_result_id uuid,
    universe_manifest_hash character(64),
    result_hash character(64),
    CONSTRAINT strategy_forecast_check CHECK ((as_of = input_cutoff)),
    CONSTRAINT strategy_forecast_check1 CHECK (((forecast_value IS NOT NULL) OR (forecast_range IS NOT NULL) OR (forecast_distribution IS NOT NULL))),
    CONSTRAINT strategy_forecast_p3_link_check CHECK ((((research_trial_id IS NULL) AND (trial_result_id IS NULL) AND (universe_manifest_hash IS NULL) AND (result_hash IS NULL)) OR ((research_trial_id IS NOT NULL) AND (trial_result_id IS NOT NULL) AND (universe_manifest_hash IS NOT NULL) AND (result_hash IS NOT NULL)))),
    CONSTRAINT strategy_forecast_p3_pit_check CHECK (((research_trial_id IS NULL) OR (available_at <= input_cutoff)))
);

CREATE TABLE analysis.strategy_manifest (
    strategy_revision_id bigint NOT NULL,
    source_definition_version text NOT NULL,
    source_manifest jsonb NOT NULL,
    data_manifest jsonb NOT NULL,
    cost_manifest jsonb NOT NULL,
    capacity_manifest jsonb NOT NULL,
    failure_manifest jsonb NOT NULL,
    manifest_hash character(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    available_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT strategy_manifest_capacity_manifest_check CHECK (((jsonb_typeof(capacity_manifest) = 'object'::text) AND (capacity_manifest <> '{}'::jsonb))),
    CONSTRAINT strategy_manifest_cost_manifest_check CHECK (((jsonb_typeof(cost_manifest) = 'object'::text) AND (cost_manifest <> '{}'::jsonb))),
    CONSTRAINT strategy_manifest_data_manifest_check CHECK (((jsonb_typeof(data_manifest) = 'object'::text) AND (data_manifest <> '{}'::jsonb))),
    CONSTRAINT strategy_manifest_failure_manifest_check CHECK (((jsonb_typeof(failure_manifest) = 'object'::text) AND (failure_manifest <> '{}'::jsonb))),
    CONSTRAINT strategy_manifest_manifest_hash_check CHECK ((manifest_hash ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT strategy_manifest_source_manifest_check CHECK (((jsonb_typeof(source_manifest) = 'object'::text) AND (source_manifest <> '{}'::jsonb)))
);

CREATE TABLE analysis.strategy_monitoring_evidence (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    strategy_revision_id bigint NOT NULL,
    research_trial_id uuid NOT NULL,
    trial_result_id uuid NOT NULL,
    universe_manifest_hash character(64) NOT NULL,
    result_hash character(64) NOT NULL,
    evidence_kind text NOT NULL,
    input_cutoff timestamp with time zone NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    input_hash character(64) NOT NULL,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    lineage jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT strategy_monitoring_evidence_check CHECK (((available_at <= observed_at) AND (available_at <= input_cutoff))),
    CONSTRAINT strategy_monitoring_evidence_evidence_kind_check CHECK ((evidence_kind = ANY (ARRAY['correlation'::text, 'tail_correlation'::text, 'crowding'::text, 'capacity'::text, 'decay'::text, 'regime'::text]))),
    CONSTRAINT strategy_monitoring_evidence_input_hash_check CHECK ((input_hash ~ '^[0-9a-f]{64}$'::text))
);

CREATE TABLE analysis.strategy_pnl_tape (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    strategy_revision_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    strategy_forecast_id text NOT NULL,
    research_trial_id uuid NOT NULL,
    trial_result_id uuid NOT NULL,
    universe_manifest_hash character(64) NOT NULL,
    result_hash character(64) NOT NULL,
    pnl_date date NOT NULL,
    input_cutoff timestamp with time zone NOT NULL,
    gross_return double precision NOT NULL,
    cost double precision NOT NULL,
    net_return double precision NOT NULL,
    tail_return double precision,
    regime text,
    observed_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    input_hash character(64) NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT strategy_pnl_tape_check CHECK (((available_at <= observed_at) AND (available_at <= input_cutoff))),
    CONSTRAINT strategy_pnl_tape_input_hash_check CHECK ((input_hash ~ '^[0-9a-f]{64}$'::text))
);

CREATE TABLE analysis.strategy_revision (
    id bigint NOT NULL,
    strategy_key text NOT NULL,
    revision integer NOT NULL,
    name text NOT NULL,
    status text NOT NULL,
    parameters jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    promoted_at timestamp with time zone,
    supersedes_id bigint,
    authority_group text NOT NULL,
    hypothesis_id uuid,
    experiment_family_id uuid,
    artifact_id text,
    artifact_hash character(64),
    research_required boolean DEFAULT false NOT NULL,
    mechanism_class text,
    economic_mechanism text,
    falsification_rule text,
    source_definition_version text,
    strategy_family text DEFAULT 'legacy'::text NOT NULL,
    promotability text DEFAULT 'standard'::text NOT NULL,
    actionability text DEFAULT 'daily_research'::text NOT NULL,
    p3_enabled boolean DEFAULT false NOT NULL,
    CONSTRAINT strategy_revision_actionability_check CHECK ((actionability = ANY (ARRAY['daily_research'::text, 'shadow_only'::text, 'research_only'::text, 'registration_only'::text]))),
    CONSTRAINT strategy_revision_family_check CHECK ((strategy_family <> ''::text)),
    CONSTRAINT strategy_revision_promotability_check CHECK ((promotability = ANY (ARRAY['standard'::text, 'negative_control'::text, 'registration_only'::text, 'exposure_sleeve'::text])))
);

CREATE VIEW analysis.strategy_registry AS
 SELECT revision.id AS strategy_revision_id,
    revision.strategy_key,
    revision.revision,
    revision.name,
    revision.status,
    revision.mechanism_class,
    revision.economic_mechanism,
    revision.falsification_rule,
    revision.source_definition_version,
    revision.strategy_family,
    revision.promotability,
    revision.actionability,
    revision.p3_enabled,
    revision.parameters,
    revision.created_at,
    revision.promoted_at,
    revision.supersedes_id,
    manifest.manifest_hash,
    manifest.available_at AS manifest_available_at
   FROM (analysis.strategy_revision revision
     LEFT JOIN analysis.strategy_manifest manifest ON ((manifest.strategy_revision_id = revision.id)));

ALTER TABLE analysis.strategy_revision ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME analysis.strategy_revision_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE analysis.trial_result (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    research_trial_id uuid NOT NULL,
    result_kind text NOT NULL,
    result_version integer DEFAULT 1 NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    outcome jsonb DEFAULT '{}'::jsonb NOT NULL,
    input_hash character(64) NOT NULL,
    CONSTRAINT trial_result_check CHECK ((available_at <= observed_at)),
    CONSTRAINT trial_result_result_version_check CHECK ((result_version > 0))
);

CREATE TABLE analysis.trial_universe_manifest (
    research_trial_id uuid NOT NULL,
    cutoff timestamp with time zone NOT NULL,
    expected_member_count integer NOT NULL,
    expected_members jsonb NOT NULL,
    manifest_hash character(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT trial_universe_manifest_expected_member_count_check CHECK (((expected_member_count >= 0) AND (expected_member_count <= 10000))),
    CONSTRAINT trial_universe_manifest_expected_members_check CHECK ((jsonb_typeof(expected_members) = 'array'::text))
);

CREATE TABLE analysis.universe_observation (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    research_trial_id uuid NOT NULL,
    instrument_id bigint NOT NULL,
    cutoff timestamp with time zone NOT NULL,
    eligible boolean NOT NULL,
    rank integer,
    candidate_score double precision,
    exclusion_reason text,
    observed_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    input_hash character(64) NOT NULL,
    outcome jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT universe_observation_check CHECK ((eligible OR (exclusion_reason IS NOT NULL))),
    CONSTRAINT universe_observation_rank_check CHECK (((rank IS NULL) OR (rank > 0)))
);

CREATE VIEW analysis.strategy_trial_accounting AS
 SELECT family.family_key,
    trial.id AS research_trial_id,
    trial.trial_key,
    trial.status,
    trial.input_cutoff,
    trial.available_at,
    manifest.expected_member_count,
    count(observation.id) AS observed_member_count,
    count(observation.id) FILTER (WHERE (observation.outcome <> '{}'::jsonb)) AS outcome_member_count,
    (EXISTS ( SELECT 1
           FROM analysis.trial_result result
          WHERE (result.research_trial_id = trial.id))) AS has_result,
    analysis.research_trial_p3_denominator_complete(trial.id) AS denominator_complete,
    analysis.research_family_complete(trial.experiment_family_id) AS family_complete
   FROM (((analysis.research_trial trial
     JOIN analysis.experiment_family family ON ((family.id = trial.experiment_family_id)))
     LEFT JOIN analysis.trial_universe_manifest manifest ON ((manifest.research_trial_id = trial.id)))
     LEFT JOIN analysis.universe_observation observation ON ((observation.research_trial_id = trial.id)))
  GROUP BY family.family_key, trial.id, trial.trial_key, trial.status, trial.input_cutoff, trial.available_at, manifest.expected_member_count, trial.experiment_family_id;

CREATE TABLE analysis.symbol_decision (
    decision_id uuid NOT NULL,
    action text,
    discovery_reasons text[] DEFAULT '{}'::text[] NOT NULL,
    freshness_status text,
    portfolio_context jsonb DEFAULT '{}'::jsonb NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE analysis.symbol_decision_outcome (
    decision_id uuid NOT NULL,
    instrument_id bigint NOT NULL,
    as_of timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    outcome_version text DEFAULT 'equity-v1'::text NOT NULL,
    state text DEFAULT 'observing'::text NOT NULL,
    return_1d double precision,
    return_5d double precision,
    return_20d double precision,
    spy_adjusted_return_1d double precision,
    spy_adjusted_return_5d double precision,
    spy_adjusted_return_20d double precision,
    sector_adjusted_return_1d double precision,
    sector_adjusted_return_5d double precision,
    sector_adjusted_return_20d double precision,
    mae double precision,
    mfe double precision,
    max_drawdown double precision,
    thesis_invalidated_at timestamp with time zone,
    sample_eligible boolean DEFAULT false NOT NULL,
    quarantine_reason text,
    measured_through timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT symbol_decision_outcome_state_check CHECK ((state = ANY (ARRAY['observing'::text, 'resolved'::text, 'quarantined'::text])))
);

CREATE TABLE analysis.symbol_feature (
    id bigint NOT NULL,
    run_id uuid NOT NULL,
    instrument_id bigint NOT NULL,
    as_of timestamp with time zone NOT NULL,
    feature_set text NOT NULL,
    feature_version text NOT NULL,
    price double precision,
    ma_50 double precision,
    ma_200 double precision,
    relative_strength_20d double precision,
    atr_pct double precision,
    liquidity_score double precision,
    valuation_score double precision,
    earnings_score double precision,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    momentum_5d double precision,
    momentum_20d double precision,
    relative_strength_60d double precision,
    kaufman_er_20d double precision,
    kaufman_er_60d double precision,
    kama_fast double precision,
    kama_slow double precision,
    kama_fast_slope double precision,
    kama_slow_slope double precision,
    trend_state text DEFAULT 'unavailable'::text NOT NULL,
    trend_confidence double precision DEFAULT 0 NOT NULL,
    volatility_state text DEFAULT 'unstable'::text NOT NULL,
    data_quality_status text DEFAULT 'unavailable'::text NOT NULL,
    reason_codes text[] DEFAULT '{}'::text[] NOT NULL,
    CONSTRAINT ck_symbol_feature_trend_confidence CHECK (((trend_confidence >= (0)::double precision) AND (trend_confidence <= (1)::double precision))),
    CONSTRAINT ck_symbol_feature_trend_state CHECK ((trend_state = ANY (ARRAY['trend_up'::text, 'trend_down'::text, 'range'::text, 'transition'::text, 'unavailable'::text]))),
    CONSTRAINT ck_symbol_feature_volatility_state CHECK ((volatility_state = ANY (ARRAY['low'::text, 'normal'::text, 'high'::text, 'unstable'::text])))
);

ALTER TABLE analysis.symbol_feature ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME analysis.symbol_feature_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE analysis.ticker_benchmark_snapshot (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    benchmark_key text NOT NULL,
    as_of timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    membership_hash character(64) NOT NULL,
    member_count integer NOT NULL,
    source_id text NOT NULL,
    source_version text,
    exact_membership jsonb DEFAULT '[]'::jsonb NOT NULL,
    coverage jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ticker_benchmark_snapshot_member_count_check CHECK ((member_count >= 0))
);

CREATE TABLE analysis.ticker_data_request (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    ticker_decision_id uuid NOT NULL,
    field text NOT NULL,
    ticker text NOT NULL,
    request jsonb NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT ticker_data_request_status_check CHECK ((status = ANY (ARRAY['open'::text, 'running'::text, 'complete'::text, 'failed'::text, 'superseded'::text])))
);

CREATE TABLE analysis.ticker_decision (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    instrument_id bigint NOT NULL,
    decision_revision text NOT NULL,
    contract_version text NOT NULL,
    as_of timestamp with time zone NOT NULL,
    published_at timestamp with time zone,
    input_hash character(64) NOT NULL,
    code_version text NOT NULL,
    experiment_id text NOT NULL,
    tactical jsonb NOT NULL,
    fundamental jsonb NOT NULL,
    capital_action jsonb NOT NULL,
    risk_policy jsonb NOT NULL,
    expressions jsonb DEFAULT '{}'::jsonb NOT NULL,
    selected_expression jsonb,
    data_requests jsonb DEFAULT '[]'::jsonb NOT NULL,
    learning_history jsonb DEFAULT '[]'::jsonb NOT NULL,
    input_manifest jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'published'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolution jsonb DEFAULT '{}'::jsonb NOT NULL,
    policy_version text DEFAULT 'risk-policy.v2:legacy'::text NOT NULL,
    opportunity_episode_id text,
    opportunity_cutoff timestamp with time zone,
    opportunity_episode jsonb DEFAULT '{}'::jsonb NOT NULL,
    market_state_publication_id uuid,
    market_state_snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    portfolio_impacts jsonb DEFAULT '{}'::jsonb NOT NULL,
    risk_policy_snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ticker_decision_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'published'::text, 'superseded'::text, 'quarantined'::text])))
);

CREATE TABLE analysis.ticker_input_manifest (
    id bigint NOT NULL,
    ticker_decision_id uuid NOT NULL,
    field text NOT NULL,
    source_id text NOT NULL,
    source_version text,
    event_at timestamp with time zone,
    published_at timestamp with time zone,
    available_at timestamp with time zone NOT NULL,
    received_at timestamp with time zone,
    revision text,
    license text,
    original_value jsonb,
    revised_value jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE analysis.ticker_input_manifest ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME analysis.ticker_input_manifest_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE analysis.ticker_outcome (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    ticker_decision_id uuid NOT NULL,
    horizon text NOT NULL,
    horizon_sessions integer NOT NULL,
    state text DEFAULT 'observing'::text NOT NULL,
    measured_through timestamp with time zone,
    selected_expression text,
    selected_return double precision,
    stock_counterfactual_return double precision,
    alternate_counterfactual_return double precision,
    cash_return double precision,
    sector_return double precision,
    market_return double precision,
    error_type text,
    mistake_card jsonb DEFAULT '{}'::jsonb NOT NULL,
    available_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ticker_outcome_horizon_check CHECK ((horizon = ANY (ARRAY['TACTICAL'::text, 'FUNDAMENTAL'::text]))),
    CONSTRAINT ticker_outcome_horizon_sessions_check CHECK ((horizon_sessions = ANY (ARRAY[1, 5, 20, 63, 126, 252]))),
    CONSTRAINT ticker_outcome_state_check CHECK ((state = ANY (ARRAY['observing'::text, 'resolved'::text, 'quarantined'::text, 'unmeasurable'::text])))
);

CREATE TABLE analysis.validation_dossier (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    strategy_revision_id bigint NOT NULL,
    research_trial_id uuid,
    status text DEFAULT 'draft'::text NOT NULL,
    sections jsonb DEFAULT '{}'::jsonb NOT NULL,
    compiled_policy jsonb DEFAULT '{}'::jsonb NOT NULL,
    artifact_id text,
    artifact_hash character(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sealed_at timestamp with time zone,
    CONSTRAINT validation_dossier_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'sealed'::text, 'rejected'::text])))
);

CREATE TABLE analysis.validation_gate_result (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    dossier_id uuid NOT NULL,
    gate_code text NOT NULL,
    verdict text NOT NULL,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    evaluated_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    CONSTRAINT validation_gate_result_check CHECK ((available_at <= evaluated_at)),
    CONSTRAINT validation_gate_result_gate_code_check CHECK ((gate_code = ANY (ARRAY['pit_integrity'::text, 'denominator_completeness'::text, 'oos_predictive_validity'::text, 'falsification_and_robustness'::text, 'economic_promotability'::text]))),
    CONSTRAINT validation_gate_result_verdict_check CHECK ((verdict = ANY (ARRAY['pass'::text, 'fail'::text, 'unavailable'::text])))
);

CREATE TABLE app.alert (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    decision_id uuid,
    instrument_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    alert_type text NOT NULL,
    severity text NOT NULL,
    title text NOT NULL,
    detail text,
    acknowledged_at timestamp with time zone,
    resolution_reason text
);

CREATE TABLE app.catalyst (
    id bigint NOT NULL,
    instrument_id bigint,
    market_event_id bigint,
    starts_at timestamp with time zone NOT NULL,
    title text NOT NULL,
    expected_impact text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    event_key text,
    version integer DEFAULT 1 NOT NULL,
    status text DEFAULT 'current'::text NOT NULL,
    supersedes_id bigint,
    superseded_at timestamp with time zone,
    source_id text,
    source_priority integer DEFAULT 0 NOT NULL,
    confidence double precision
);

ALTER TABLE app.catalyst ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME app.catalyst_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE app.current_publication_item (
    scope text NOT NULL,
    publication_id uuid NOT NULL,
    model_name text NOT NULL,
    stable_key text NOT NULL,
    rank integer NOT NULL,
    instrument_id bigint,
    content_hash character(64) NOT NULL
);

CREATE TABLE app.decision_inbox_item (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    dedupe_key text NOT NULL,
    event_type text NOT NULL,
    opportunity_id uuid,
    ticket_version integer,
    paper_order_id uuid,
    lane text,
    severity text DEFAULT 'info'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    user_state text DEFAULT 'open'::text NOT NULL,
    snoozed_until timestamp with time zone,
    dismiss_reason text,
    user_state_updated_at timestamp with time zone,
    reviewed_at timestamp with time zone,
    CONSTRAINT ck_app_decision_inbox_event_type CHECK ((event_type = ANY (ARRAY['ready'::text, 'revoked'::text, 'expired'::text, 'paper_filled'::text, 'paper_exited'::text, 'portfolio_critical'::text, 'paper_engine_halt'::text, 'high_priority_research'::text]))),
    CONSTRAINT ck_decision_inbox_dismiss_reason CHECK (((user_state <> 'dismissed'::text) OR (NULLIF(btrim(dismiss_reason), ''::text) IS NOT NULL))),
    CONSTRAINT ck_decision_inbox_snooze_state CHECK (((user_state <> 'snoozed'::text) OR (snoozed_until IS NOT NULL))),
    CONSTRAINT ck_decision_inbox_user_state CHECK ((user_state = ANY (ARRAY['open'::text, 'acknowledged'::text, 'snoozed'::text, 'dismissed'::text, 'review_complete'::text]))),
    CONSTRAINT decision_inbox_item_severity_check CHECK ((severity = ANY (ARRAY['info'::text, 'warning'::text, 'critical'::text]))),
    CONSTRAINT decision_inbox_item_status_check CHECK ((status = ANY (ARRAY['active'::text, 'resolved'::text])))
);

CREATE TABLE app.decision_inbox_sync_state (
    state_key text NOT NULL,
    activated_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE app.decision_truth (
    symbol text NOT NULL,
    lane text NOT NULL,
    as_of timestamp with time zone NOT NULL,
    publication_id text,
    candidate_state text,
    route_verdict text,
    readiness_state text,
    execution_state text,
    primary_blocker text,
    blockers jsonb DEFAULT '[]'::jsonb NOT NULL,
    next_action text,
    route_version text,
    evidence_refs jsonb DEFAULT '[]'::jsonb NOT NULL,
    event_id text,
    raw jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_decision_truth_json_arrays CHECK (((jsonb_typeof(blockers) = 'array'::text) AND (jsonb_typeof(evidence_refs) = 'array'::text)))
);

CREATE TABLE app.manual_account_snapshot (
    id bigint NOT NULL,
    account_key text DEFAULT 'manual'::text NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    effective_at timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    cash_balance numeric(20,4) NOT NULL,
    net_liquidation numeric(20,4),
    reconciliation_state text NOT NULL,
    reconciliation_version integer NOT NULL,
    ledger_book_identity text NOT NULL,
    idempotency_key text NOT NULL,
    notes text DEFAULT ''::text NOT NULL,
    CONSTRAINT manual_account_snapshot_cash_balance_check CHECK ((cash_balance >= (0)::numeric)),
    CONSTRAINT manual_account_snapshot_currency_check CHECK ((currency = 'USD'::text)),
    CONSTRAINT manual_account_snapshot_net_liquidation_check CHECK ((net_liquidation >= (0)::numeric)),
    CONSTRAINT manual_account_snapshot_reconciliation_state_check CHECK ((reconciliation_state = ANY (ARRAY['pending'::text, 'reconciled'::text]))),
    CONSTRAINT manual_account_snapshot_reconciliation_version_check CHECK ((reconciliation_version > 0))
);

ALTER TABLE app.manual_account_snapshot ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME app.manual_account_snapshot_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE app.notification_outbox (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    dedupe_key text NOT NULL,
    inbox_item_id uuid NOT NULL,
    channel text DEFAULT 'telegram_owner'::text NOT NULL,
    event_type text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    last_error text,
    sent_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT notification_outbox_attempts_check CHECK ((attempts >= 0)),
    CONSTRAINT notification_outbox_channel_check CHECK ((channel = 'telegram_owner'::text)),
    CONSTRAINT notification_outbox_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'sending'::text, 'sent'::text, 'failed'::text, 'dry_run'::text, 'suppressed'::text, 'uncertain'::text])))
);

CREATE TABLE app.option_history_policy (
    instrument_id bigint NOT NULL,
    requested_state text DEFAULT 'off'::text NOT NULL,
    effective_state text DEFAULT 'disabled'::text NOT NULL,
    collection_tier text DEFAULT 'standard'::text NOT NULL,
    cadence_minutes integer DEFAULT 60 NOT NULL,
    publication_cap text DEFAULT 'WATCH'::text NOT NULL,
    provider text DEFAULT 'robinhood'::text NOT NULL,
    normalized_retention_days integer DEFAULT 730 NOT NULL,
    derived_retention_days integer DEFAULT 30 NOT NULL,
    provider_payload_retention_days integer DEFAULT 90 NOT NULL,
    policy_revision text DEFAULT 'options-chain-reliability-20260722'::text NOT NULL,
    lock_version integer DEFAULT 0 NOT NULL,
    reason text,
    activated_at timestamp with time zone,
    paused_at timestamp with time zone,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    profile text DEFAULT 'history_full'::text NOT NULL,
    activation_reason text,
    event_id uuid,
    expires_at timestamp with time zone,
    hot_retention_days integer DEFAULT 7 NOT NULL,
    archive_retention_days integer DEFAULT 730 NOT NULL,
    CONSTRAINT ck_option_history_policy_cadence CHECK ((cadence_minutes = ANY (ARRAY[15, 60]))),
    CONSTRAINT ck_option_history_policy_cap CHECK ((publication_cap = ANY (ARRAY['WATCH'::text, 'PAPER_READY'::text]))),
    CONSTRAINT ck_option_history_policy_effective CHECK ((effective_state = ANY (ARRAY['disabled'::text, 'pending_gate'::text, 'shadow'::text, 'active'::text, 'paused'::text]))),
    CONSTRAINT ck_option_history_policy_profile CHECK ((profile = ANY (ARRAY['history_full'::text, 'event_strip'::text]))),
    CONSTRAINT ck_option_history_policy_requested CHECK ((requested_state = ANY (ARRAY['on'::text, 'off'::text]))),
    CONSTRAINT ck_option_history_policy_retention CHECK ((((profile = 'history_full'::text) AND (normalized_retention_days = 730) AND (derived_retention_days = 30) AND (provider_payload_retention_days = 90) AND (event_id IS NULL)) OR ((profile = 'event_strip'::text) AND (normalized_retention_days = 365) AND (derived_retention_days = 30) AND (provider_payload_retention_days = 30) AND (event_id IS NOT NULL)))),
    CONSTRAINT ck_option_history_policy_tier CHECK ((collection_tier = ANY (ARRAY['core'::text, 'standard'::text, 'event'::text]))),
    CONSTRAINT option_history_policy_archive_retention_days_check CHECK ((archive_retention_days >= 0)),
    CONSTRAINT option_history_policy_hot_retention_days_check CHECK ((hot_retention_days >= 0))
);

CREATE TABLE app.paper_execution_observation (
    paper_execution_observation_id text CONSTRAINT paper_execution_observation_paper_execution_observatio_not_null NOT NULL,
    allocation_item_id text NOT NULL,
    action_id text NOT NULL,
    paper_order_id uuid NOT NULL,
    execution_mode text DEFAULT 'paper'::text NOT NULL,
    paper_only boolean DEFAULT true NOT NULL,
    status text NOT NULL,
    requested_quantity double precision NOT NULL,
    filled_quantity double precision NOT NULL,
    requested_price double precision,
    fill_price double precision,
    spread_bps double precision,
    latency_ms double precision,
    impact_bps double precision,
    side text DEFAULT 'buy'::text NOT NULL,
    exit_price double precision,
    observed_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    event_fee double precision,
    contract_multiplier double precision,
    CONSTRAINT paper_execution_observation_check CHECK (((filled_quantity < 'Infinity'::double precision) AND (filled_quantity > '-Infinity'::double precision) AND (filled_quantity >= (0)::double precision) AND (filled_quantity <= requested_quantity))),
    CONSTRAINT paper_execution_observation_check1 CHECK (((status <> ALL (ARRAY['filled'::text, 'exited'::text])) OR (filled_quantity > (0)::double precision))),
    CONSTRAINT paper_execution_observation_check2 CHECK (((fill_price IS NOT NULL) OR (filled_quantity = (0)::double precision))),
    CONSTRAINT paper_execution_observation_check3 CHECK ((available_at >= observed_at)),
    CONSTRAINT paper_execution_observation_execution_mode_check CHECK ((execution_mode = 'paper'::text)),
    CONSTRAINT paper_execution_observation_exit_price_check CHECK (((exit_price IS NULL) OR ((exit_price < 'Infinity'::double precision) AND (exit_price >= (0)::double precision)))),
    CONSTRAINT paper_execution_observation_fill_price_check CHECK (((fill_price IS NULL) OR ((fill_price < 'Infinity'::double precision) AND (fill_price > '-Infinity'::double precision) AND (fill_price > (0)::double precision)))),
    CONSTRAINT paper_execution_observation_impact_bps_check CHECK (((impact_bps IS NULL) OR ((impact_bps < 'Infinity'::double precision) AND (impact_bps > '-Infinity'::double precision) AND (impact_bps >= (0)::double precision)))),
    CONSTRAINT paper_execution_observation_latency_ms_check CHECK (((latency_ms IS NULL) OR ((latency_ms < 'Infinity'::double precision) AND (latency_ms > '-Infinity'::double precision) AND (latency_ms >= (0)::double precision)))),
    CONSTRAINT paper_execution_observation_paper_only_check CHECK (paper_only),
    CONSTRAINT paper_execution_observation_requested_price_check CHECK (((requested_price IS NULL) OR ((requested_price < 'Infinity'::double precision) AND (requested_price > '-Infinity'::double precision) AND (requested_price > (0)::double precision)))),
    CONSTRAINT paper_execution_observation_requested_quantity_check CHECK (((requested_quantity < 'Infinity'::double precision) AND (requested_quantity > '-Infinity'::double precision) AND (requested_quantity >= (0)::double precision))),
    CONSTRAINT paper_execution_observation_side_check CHECK ((side = ANY (ARRAY['buy'::text, 'sell'::text]))),
    CONSTRAINT paper_execution_observation_spread_bps_check CHECK (((spread_bps IS NULL) OR ((spread_bps < 'Infinity'::double precision) AND (spread_bps > '-Infinity'::double precision) AND (spread_bps >= (0)::double precision)))),
    CONSTRAINT phase4_paper_observation_status CHECK ((status = ANY (ARRAY['planned'::text, 'submitted'::text, 'partial'::text, 'filled'::text, 'partial_exited'::text, 'exited'::text, 'cancelled'::text, 'unavailable'::text])))
);

CREATE TABLE app.paper_order (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    decision_id uuid,
    instrument_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    side text NOT NULL,
    quantity numeric(24,8) NOT NULL,
    limit_price numeric(20,6),
    status text NOT NULL,
    policy_result jsonb DEFAULT '{}'::jsonb NOT NULL,
    structure text,
    reserved_collateral numeric(24,4),
    idempotency_key text,
    ticket_version integer,
    ticket_snapshot jsonb,
    intended_limit_price numeric(20,6),
    actual_fill_price numeric(20,6),
    filled_at timestamp with time zone,
    event_id uuid,
    event_signal_id uuid,
    strategy_family text,
    objective_version text,
    entry_capture_count integer DEFAULT 0 NOT NULL,
    cohort_id uuid,
    lane text DEFAULT 'radar'::text NOT NULL,
    policy_snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    exit_at timestamp with time zone,
    exit_price numeric(20,6),
    fees numeric(20,6) DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    submitted_at timestamp with time zone,
    filled_quantity numeric(24,8),
    exited_quantity numeric(24,8) DEFAULT 0 NOT NULL,
    entry_slippage numeric(20,6),
    exit_slippage numeric(20,6),
    unfilled_reason text,
    ticker_decision_id uuid,
    ticker_decision_revision text,
    expression_kind text,
    max_loss numeric(20,6),
    planned_loss numeric(20,6),
    expires_at timestamp with time zone,
    thesis_snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    paper_only boolean DEFAULT true NOT NULL,
    execution_quote jsonb,
    fill_evidence_at timestamp with time zone,
    contract_multiplier numeric(20,6),
    entry_fees numeric(20,6) DEFAULT 0 NOT NULL,
    exit_fees numeric(20,6) DEFAULT 0 NOT NULL,
    CONSTRAINT ck_app_paper_order_lane CHECK ((lane = ANY (ARRAY['radar'::text, 'qqq'::text, 'recovery'::text, 'ticker'::text]))),
    CONSTRAINT ck_paper_order_entry_capture_count CHECK ((entry_capture_count >= 0))
);

CREATE TABLE app.paper_order_leg (
    paper_order_id uuid NOT NULL,
    leg_index integer NOT NULL,
    contract_id bigint NOT NULL,
    option_type text NOT NULL,
    side text NOT NULL,
    strike numeric(20,6) NOT NULL,
    bid numeric(20,6) NOT NULL,
    ask numeric(20,6) NOT NULL,
    bid_size integer NOT NULL,
    ask_size integer NOT NULL,
    quote_time timestamp with time zone NOT NULL,
    open_interest integer,
    volume integer
);

CREATE TABLE app.portfolio_position (
    instrument_id bigint NOT NULL,
    quantity numeric(24,8) NOT NULL,
    average_cost numeric(20,6),
    purchase_date date,
    notes text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE app.portfolio_transaction (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    instrument_id bigint,
    transaction_type text NOT NULL,
    quantity numeric(24,8),
    price numeric(20,6),
    amount numeric(20,6),
    fees numeric(20,6) DEFAULT '0'::numeric NOT NULL,
    realized_pnl numeric(20,6) DEFAULT '0'::numeric NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    account text DEFAULT 'manual'::text NOT NULL,
    executed_at timestamp with time zone NOT NULL,
    notes text DEFAULT ''::text NOT NULL,
    idempotency_key text NOT NULL,
    reverses_transaction_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    instrument_sector text,
    CONSTRAINT ck_portfolio_transaction_fees_nonnegative CHECK ((fees >= (0)::numeric)),
    CONSTRAINT ck_portfolio_transaction_type CHECK ((transaction_type = ANY (ARRAY['opening_balance'::text, 'buy'::text, 'sell'::text, 'dividend'::text, 'fee'::text, 'split'::text, 'transfer_in'::text, 'transfer_out'::text, 'cash_deposit'::text, 'cash_withdrawal'::text])))
);

CREATE TABLE app.publication (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    scope text NOT NULL,
    analysis_run_id uuid NOT NULL,
    status text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    published_at timestamp with time zone,
    validation jsonb DEFAULT '{}'::jsonb NOT NULL,
    bundle_id uuid,
    CONSTRAINT publication_status_check CHECK ((status = ANY (ARRAY['building'::text, 'published'::text, 'failed'::text, 'superseded'::text])))
);

CREATE TABLE app.publication_bundle (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    scope text NOT NULL,
    bundle_hash character(64) NOT NULL,
    item_count integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT publication_bundle_item_count_check CHECK ((item_count >= 0))
);

CREATE TABLE app.publication_bundle_item (
    bundle_id uuid NOT NULL,
    model_name text NOT NULL,
    stable_key text NOT NULL,
    rank integer NOT NULL,
    instrument_id bigint,
    content_hash character(64) NOT NULL
);

CREATE TABLE app.publication_item (
    publication_id uuid NOT NULL,
    model_name text NOT NULL,
    stable_key text NOT NULL,
    rank integer NOT NULL,
    instrument_id bigint,
    payload jsonb NOT NULL
);

CREATE TABLE app.publication_payload (
    content_hash character(64) NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE VIEW app.publication_content_item AS
 SELECT item.publication_id,
    item.model_name,
    item.stable_key,
    item.rank,
    item.instrument_id,
    item.payload
   FROM (app.publication_item item
     JOIN app.publication publication ON ((publication.id = item.publication_id)))
  WHERE (publication.bundle_id IS NULL)
UNION ALL
 SELECT publication.id AS publication_id,
    item.model_name,
    item.stable_key,
    item.rank,
    item.instrument_id,
    payload.payload
   FROM ((app.publication publication
     JOIN app.publication_bundle_item item ON ((item.bundle_id = publication.bundle_id)))
     JOIN app.publication_payload payload ON ((payload.content_hash = item.content_hash)));

CREATE TABLE app.research_report (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    instrument_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    report_type text NOT NULL,
    markdown text,
    report jsonb NOT NULL,
    evidence jsonb DEFAULT '[]'::jsonb NOT NULL
);

CREATE TABLE app.setting (
    key text NOT NULL,
    value jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE app.thesis (
    id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    revision integer NOT NULL,
    status text NOT NULL,
    thesis jsonb NOT NULL,
    source_agent_task_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    schema_version integer DEFAULT 3 NOT NULL,
    author_kind text DEFAULT 'legacy'::text NOT NULL,
    automation_run_id uuid,
    superseded_revision_id bigint,
    change_rationale text,
    last_assessed_at timestamp with time zone,
    last_human_reviewed_at timestamp with time zone
);

CREATE TABLE app.thesis_automation_run (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    instrument_id bigint,
    run_kind text DEFAULT 'assessment'::text NOT NULL,
    trigger text DEFAULT 'manual'::text NOT NULL,
    model text,
    reasoning_effort text,
    prompt_version text DEFAULT 'thesis_v3_20260725'::text NOT NULL,
    evidence_fingerprint text,
    evidence_snapshot jsonb DEFAULT '[]'::jsonb NOT NULL,
    input_symbol text,
    input_tokens integer,
    output_tokens integer,
    cost_usd numeric(12,6),
    status text NOT NULL,
    error text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT thesis_automation_run_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'succeeded'::text, 'failed'::text, 'timeout'::text, 'skipped'::text])))
);

CREATE TABLE app.thesis_evidence_assessment (
    id bigint NOT NULL,
    thesis_revision_id bigint,
    automation_run_id uuid,
    instrument_id bigint NOT NULL,
    evidence_reference text NOT NULL,
    evidence_title text,
    evidence_date timestamp with time zone,
    stance text NOT NULL,
    materiality text DEFAULT 'low'::text NOT NULL,
    affected_pillar_ids text[] DEFAULT ARRAY[]::text[] NOT NULL,
    confidence numeric(5,4) DEFAULT 0 NOT NULL,
    rationale text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT thesis_evidence_assessment_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT thesis_evidence_assessment_materiality_check CHECK ((materiality = ANY (ARRAY['low'::text, 'medium'::text, 'high'::text]))),
    CONSTRAINT thesis_evidence_assessment_stance_check CHECK ((stance = ANY (ARRAY['support'::text, 'contradict'::text, 'neutral'::text, 'insufficient'::text])))
);

ALTER TABLE app.thesis_evidence_assessment ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME app.thesis_evidence_assessment_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE app.thesis_expression (
    id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    thesis_revision_id bigint NOT NULL,
    expression_kind text NOT NULL,
    structure jsonb DEFAULT '{}'::jsonb NOT NULL,
    entry_logic jsonb DEFAULT '{}'::jsonb NOT NULL,
    max_loss numeric(20,6),
    risk_budget numeric(20,6),
    horizon_date date,
    invalidation_rules jsonb DEFAULT '[]'::jsonb NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT thesis_expression_expression_kind_check CHECK ((expression_kind = ANY (ARRAY['equity'::text, 'option'::text])))
);

ALTER TABLE app.thesis_expression ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME app.thesis_expression_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

ALTER TABLE app.thesis ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME app.thesis_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE app.thesis_review_event (
    id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    thesis_revision_id bigint,
    outcome text NOT NULL,
    notes text,
    reviewed_evidence_cutoff timestamp with time zone,
    reviewed_by text DEFAULT 'joe'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT thesis_review_event_outcome_check CHECK ((outcome = ANY (ARRAY['unchanged'::text, 'updated'::text, 'invalidated'::text, 'closed'::text, 'legacy_acknowledgement'::text])))
);

ALTER TABLE app.thesis_review_event ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME app.thesis_review_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE app.trade_journal (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    decision_id uuid,
    instrument_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    action text NOT NULL,
    quantity numeric(24,8),
    price numeric(20,6),
    rationale text,
    details jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE app.watchlist_item (
    instrument_id bigint NOT NULL,
    watch_state text NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE catalog.instrument (
    id bigint NOT NULL,
    symbol text NOT NULL,
    name text,
    asset_class text NOT NULL,
    sector text,
    industry text,
    category text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    market_timezone text DEFAULT 'America/New_York'::text NOT NULL,
    delisted_at timestamp with time zone,
    delisting_price double precision,
    delisting_available_at timestamp with time zone,
    delisting_source text,
    CONSTRAINT ck_instrument_delisting_availability CHECK (((delisting_available_at IS NULL) OR (delisted_at IS NOT NULL))),
    CONSTRAINT ck_instrument_delisting_price CHECK (((delisting_price IS NULL) OR (delisting_price > (0)::double precision)))
);

CREATE TABLE catalog.instrument_alias (
    id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    provider text NOT NULL,
    external_symbol text NOT NULL,
    exchange text DEFAULT ''::text NOT NULL,
    currency text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE catalog.instrument_alias ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME catalog.instrument_alias_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

ALTER TABLE catalog.instrument ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME catalog.instrument_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE catalog.option_contract (
    id bigint NOT NULL,
    underlying_instrument_id bigint NOT NULL,
    expiration date NOT NULL,
    strike numeric(20,6) NOT NULL,
    option_type text NOT NULL,
    multiplier integer DEFAULT 100 NOT NULL,
    style text,
    settlement text,
    provider_symbols jsonb DEFAULT '{}'::jsonb NOT NULL,
    deliverable_key text NOT NULL,
    standard_contract_verified boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_option_contract_standard_terms CHECK (((NOT standard_contract_verified) OR ((style = 'american'::text) AND (settlement = 'physical'::text) AND (deliverable_key IS NOT NULL)))),
    CONSTRAINT option_contract_multiplier_check CHECK ((multiplier > 0)),
    CONSTRAINT option_contract_option_type_check CHECK ((option_type = ANY (ARRAY['call'::text, 'put'::text])))
);

ALTER TABLE catalog.option_contract ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME catalog.option_contract_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE ingest.payload (
    id bigint NOT NULL,
    run_id uuid NOT NULL,
    archive_uri text NOT NULL,
    sha256 character(64) NOT NULL,
    encoding text NOT NULL,
    byte_count bigint NOT NULL,
    schema_version text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT payload_byte_count_check CHECK ((byte_count >= 0))
);

ALTER TABLE ingest.payload ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ingest.payload_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE ingest.run (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_id text NOT NULL,
    source_run_key text,
    capability text NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    status text NOT NULL,
    item_count integer DEFAULT 0 NOT NULL,
    instrument_count integer DEFAULT 0 NOT NULL,
    failure_detail text,
    summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT run_status_check CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'partial'::text, 'failed'::text, 'skipped'::text])))
);

CREATE TABLE ingest.source (
    id text NOT NULL,
    name text NOT NULL,
    family text NOT NULL,
    kind text NOT NULL,
    origin text,
    enabled boolean DEFAULT true NOT NULL,
    ingestion_mode text,
    source_url text,
    capabilities jsonb DEFAULT '{}'::jsonb NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    operational_state text DEFAULT 'archived'::text NOT NULL,
    health_owner text,
    freshness_seconds integer,
    CONSTRAINT ck_ingest_source_active_health_contract CHECK (((operational_state <> 'active'::text) OR ((health_owner IS NOT NULL) AND (freshness_seconds IS NOT NULL)))),
    CONSTRAINT ck_ingest_source_freshness_seconds CHECK (((freshness_seconds IS NULL) OR (freshness_seconds > 0))),
    CONSTRAINT ck_ingest_source_operational_state CHECK ((operational_state = ANY (ARRAY['active'::text, 'standby'::text, 'archived'::text])))
);

CREATE TABLE ingest.source_lifecycle_history (
    id bigint NOT NULL,
    source_id text NOT NULL,
    effective_at timestamp with time zone NOT NULL,
    enabled boolean NOT NULL,
    operational_state text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ingest.source_lifecycle_history ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ingest.source_lifecycle_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE ops.job_run (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_name text NOT NULL,
    status text NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    error text,
    summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    heartbeat_at timestamp with time zone DEFAULT now() NOT NULL,
    scheduled_due_at timestamp with time zone,
    dispatched_at timestamp with time zone,
    source_status text,
    downstream_status text,
    CONSTRAINT job_run_status_check CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'partial'::text, 'failed'::text, 'skipped'::text])))
);

CREATE TABLE ops.option_quote_partition_policy (
    policy_key text NOT NULL,
    daily_start date NOT NULL,
    hot_retention_days integer DEFAULT 7 NOT NULL,
    archive_retention_days integer DEFAULT 730 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE ops.provider_lease (
    id bigint NOT NULL,
    provider text NOT NULL,
    workload text NOT NULL,
    symbol text NOT NULL,
    owner text NOT NULL,
    heartbeat_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    acquired_at timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE ops.provider_lease ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ops.provider_lease_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE ops.storage_archive_checkpoint (
    checkpoint_key text NOT NULL,
    archive_kind text NOT NULL,
    source_relation text NOT NULL,
    cursor jsonb DEFAULT '{}'::jsonb NOT NULL,
    run_status text DEFAULT 'idle'::text NOT NULL,
    counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    error_detail text,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT storage_archive_checkpoint_run_status_check CHECK ((run_status = ANY (ARRAY['idle'::text, 'running'::text, 'paused'::text, 'succeeded'::text, 'failed'::text])))
);

CREATE TABLE ops.storage_archive_manifest (
    id bigint NOT NULL,
    archive_kind text NOT NULL,
    source_relation text NOT NULL,
    nas_uri text NOT NULL,
    sha256 character(64) NOT NULL,
    format text NOT NULL,
    row_count bigint DEFAULT 0 NOT NULL,
    range_start timestamp with time zone,
    range_end timestamp with time zone,
    schema_revision text NOT NULL,
    verification_status text DEFAULT 'pending'::text NOT NULL,
    verified_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT storage_archive_manifest_row_count_check CHECK ((row_count >= 0)),
    CONSTRAINT storage_archive_manifest_verification_status_check CHECK ((verification_status = ANY (ARRAY['pending'::text, 'written'::text, 'verified'::text, 'failed'::text, 'restored'::text])))
);

ALTER TABLE ops.storage_archive_manifest ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME ops.storage_archive_manifest_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE ops.storage_archive_manifest_reference (
    manifest_id bigint NOT NULL,
    source_relation text NOT NULL,
    source_row_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_ingest_run_id uuid
);

CREATE TABLE raw.broker_account_snapshot (
    id bigint NOT NULL,
    source_id text NOT NULL,
    ingest_run_id uuid NOT NULL,
    account_key text NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    currency text,
    net_liquidation numeric(20,4),
    buying_power numeric(20,4),
    cash_balance numeric(20,4),
    details jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE raw.broker_account_snapshot ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME raw.broker_account_snapshot_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE raw.broker_activity (
    id bigint NOT NULL,
    source_id text NOT NULL,
    ingest_run_id uuid NOT NULL,
    account_key text NOT NULL,
    activity_key text NOT NULL,
    activity_type text NOT NULL,
    instrument_id bigint,
    occurred_at timestamp with time zone NOT NULL,
    side text,
    quantity numeric(24,8),
    price numeric(20,6),
    status text,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT broker_activity_activity_type_check CHECK ((activity_type = ANY (ARRAY['order'::text, 'fill'::text])))
);

ALTER TABLE raw.broker_activity ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME raw.broker_activity_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE raw.broker_position_snapshot (
    id bigint NOT NULL,
    account_snapshot_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    quantity numeric(24,8) NOT NULL,
    average_cost numeric(20,6),
    market_price numeric(20,6),
    market_value numeric(24,4),
    unrealized_pnl numeric(24,4),
    details jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE raw.broker_position_snapshot ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME raw.broker_position_snapshot_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE raw.price_bar (
    id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    source_id text NOT NULL,
    ingest_run_id uuid NOT NULL,
    payload_id bigint,
    "interval" text DEFAULT '1d'::text NOT NULL,
    trading_date date NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    open double precision,
    high double precision,
    low double precision,
    close double precision NOT NULL,
    volume double precision,
    currency text,
    available_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);

CREATE TABLE raw.price_bar_fact_availability (
    fact_id bigint NOT NULL,
    fact_available_at timestamp with time zone NOT NULL,
    ingest_run_id uuid NOT NULL
);

CREATE TABLE raw.price_bar_history (
    id bigint CONSTRAINT price_bar_id_not_null NOT NULL,
    instrument_id bigint CONSTRAINT price_bar_instrument_id_not_null NOT NULL,
    source_id text CONSTRAINT price_bar_source_id_not_null NOT NULL,
    ingest_run_id uuid CONSTRAINT price_bar_ingest_run_id_not_null NOT NULL,
    payload_id bigint,
    "interval" text DEFAULT '1d'::text CONSTRAINT price_bar_interval_not_null NOT NULL,
    trading_date date CONSTRAINT price_bar_trading_date_not_null NOT NULL,
    observed_at timestamp with time zone CONSTRAINT price_bar_observed_at_not_null NOT NULL,
    open double precision,
    high double precision,
    low double precision,
    close double precision CONSTRAINT price_bar_close_not_null NOT NULL,
    volume double precision,
    currency text,
    available_at timestamp with time zone DEFAULT clock_timestamp() CONSTRAINT price_bar_available_at_not_null NOT NULL
);

CREATE VIEW raw.confirmed_price_bar AS
 SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact."interval", fact.observed_at) fact.id,
    fact.instrument_id,
    fact.source_id,
    fact.ingest_run_id,
    fact.payload_id,
    fact."interval",
    fact.trading_date,
    fact.observed_at,
    fact.open,
    fact.high,
    fact.low,
    fact.close,
    fact.volume,
    fact.currency,
    fact.available_at
   FROM ((( SELECT price_bar.id,
            price_bar.instrument_id,
            price_bar.source_id,
            price_bar.ingest_run_id,
            price_bar.payload_id,
            price_bar."interval",
            price_bar.trading_date,
            price_bar.observed_at,
            price_bar.open,
            price_bar.high,
            price_bar.low,
            price_bar.close,
            price_bar.volume,
            price_bar.currency,
            price_bar.available_at
           FROM raw.price_bar
        UNION ALL
         SELECT price_bar_history.id,
            price_bar_history.instrument_id,
            price_bar_history.source_id,
            price_bar_history.ingest_run_id,
            price_bar_history.payload_id,
            price_bar_history."interval",
            price_bar_history.trading_date,
            price_bar_history.observed_at,
            price_bar_history.open,
            price_bar_history.high,
            price_bar_history.low,
            price_bar_history.close,
            price_bar_history.volume,
            price_bar_history.currency,
            price_bar_history.available_at
           FROM raw.price_bar_history) fact
     JOIN raw.price_bar_fact_availability availability ON (((availability.fact_id = fact.id) AND (availability.fact_available_at = fact.available_at))))
     JOIN ingest.run price_run ON (((price_run.id = availability.ingest_run_id) AND (price_run.status = ANY (ARRAY['succeeded'::text, 'partial'::text])) AND (price_run.finished_at IS NOT NULL))))
  ORDER BY fact.instrument_id, fact.source_id, fact."interval", fact.observed_at, fact.available_at DESC;

CREATE TABLE raw.quote (
    id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    source_id text NOT NULL,
    ingest_run_id uuid NOT NULL,
    payload_id bigint,
    observed_at timestamp with time zone NOT NULL,
    price double precision NOT NULL,
    change_abs double precision,
    change_pct double precision,
    currency text,
    available_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);

CREATE TABLE raw.quote_fact_availability (
    fact_id bigint NOT NULL,
    fact_available_at timestamp with time zone NOT NULL,
    ingest_run_id uuid NOT NULL
);

CREATE TABLE raw.quote_history (
    id bigint CONSTRAINT quote_id_not_null NOT NULL,
    instrument_id bigint CONSTRAINT quote_instrument_id_not_null NOT NULL,
    source_id text CONSTRAINT quote_source_id_not_null NOT NULL,
    ingest_run_id uuid CONSTRAINT quote_ingest_run_id_not_null NOT NULL,
    payload_id bigint,
    observed_at timestamp with time zone CONSTRAINT quote_observed_at_not_null NOT NULL,
    price double precision CONSTRAINT quote_price_not_null NOT NULL,
    change_abs double precision,
    change_pct double precision,
    currency text,
    available_at timestamp with time zone DEFAULT clock_timestamp() CONSTRAINT quote_available_at_not_null NOT NULL
);

CREATE VIEW raw.confirmed_quote AS
 SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.observed_at) fact.id,
    fact.instrument_id,
    fact.source_id,
    fact.ingest_run_id,
    fact.payload_id,
    fact.observed_at,
    fact.price,
    fact.change_abs,
    fact.change_pct,
    fact.currency,
    fact.available_at
   FROM ((( SELECT quote.id,
            quote.instrument_id,
            quote.source_id,
            quote.ingest_run_id,
            quote.payload_id,
            quote.observed_at,
            quote.price,
            quote.change_abs,
            quote.change_pct,
            quote.currency,
            quote.available_at
           FROM raw.quote
        UNION ALL
         SELECT quote_history.id,
            quote_history.instrument_id,
            quote_history.source_id,
            quote_history.ingest_run_id,
            quote_history.payload_id,
            quote_history.observed_at,
            quote_history.price,
            quote_history.change_abs,
            quote_history.change_pct,
            quote_history.currency,
            quote_history.available_at
           FROM raw.quote_history) fact
     JOIN raw.quote_fact_availability availability ON (((availability.fact_id = fact.id) AND (availability.fact_available_at = fact.available_at))))
     JOIN ingest.run price_run ON (((price_run.id = availability.ingest_run_id) AND (price_run.status = ANY (ARRAY['succeeded'::text, 'partial'::text])) AND (price_run.finished_at IS NOT NULL))))
  ORDER BY fact.instrument_id, fact.source_id, fact.observed_at, fact.available_at DESC;

CREATE TABLE raw.content_item (
    id bigint NOT NULL,
    source_id text NOT NULL,
    ingest_run_id uuid NOT NULL,
    payload_id bigint,
    source_key text NOT NULL,
    kind text NOT NULL,
    title text,
    url text,
    author text,
    published_at timestamp with time zone,
    observed_at timestamp with time zone NOT NULL,
    summary text,
    content_hash character(64),
    license_status text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE raw.content_item ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME raw.content_item_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE raw.content_item_instrument (
    content_item_id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    relevance double precision
);

CREATE TABLE raw.disclosure (
    id bigint NOT NULL,
    instrument_id bigint,
    source_id text NOT NULL,
    ingest_run_id uuid NOT NULL,
    payload_id bigint,
    source_key text NOT NULL,
    source_type text NOT NULL,
    trader_name text,
    filer_name text,
    event_date date,
    filed_date date,
    action text,
    amount_text text,
    source_url text,
    details jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE raw.disclosure ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME raw.disclosure_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE raw.fundamental_observation (
    id bigint NOT NULL,
    instrument_id bigint NOT NULL,
    source_id text NOT NULL,
    ingest_run_id uuid NOT NULL,
    payload_id bigint,
    metric_set text NOT NULL,
    period_end date,
    filed_at timestamp with time zone,
    observed_at timestamp with time zone NOT NULL,
    "values" jsonb NOT NULL,
    period_start date
);

ALTER TABLE raw.fundamental_observation ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME raw.fundamental_observation_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE raw.market_event (
    id bigint NOT NULL,
    instrument_id bigint,
    source_id text NOT NULL,
    ingest_run_id uuid NOT NULL,
    payload_id bigint,
    source_key text NOT NULL,
    event_scope text NOT NULL,
    event_kind text NOT NULL,
    title text NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    ends_at timestamp with time zone,
    importance text,
    verification_status text,
    source_url text,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    available_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);

ALTER TABLE raw.market_event ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME raw.market_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE raw.market_event_version (
    id bigint NOT NULL,
    market_event_id bigint NOT NULL,
    instrument_id bigint,
    source_id text NOT NULL,
    ingest_run_id uuid NOT NULL,
    payload_id bigint,
    source_key text NOT NULL,
    event_scope text NOT NULL,
    event_kind text NOT NULL,
    title text NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    ends_at timestamp with time zone,
    importance text,
    verification_status text,
    source_url text,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    available_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);

ALTER TABLE raw.market_event_version ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME raw.market_event_version_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE raw.market_observation (
    observation_id text NOT NULL,
    field_name text NOT NULL,
    dimension text NOT NULL,
    asset_class text NOT NULL,
    source_id text NOT NULL,
    source_version text NOT NULL,
    value jsonb,
    unit text,
    ingest_run_id uuid NOT NULL,
    payload_id bigint NOT NULL,
    content_hash text NOT NULL,
    parent_snapshot_id text,
    observed_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    publication_at timestamp with time zone,
    release_at timestamp with time zone,
    vintage_at timestamp with time zone,
    actual double precision,
    consensus double precision,
    surprise double precision,
    revision double precision,
    status text NOT NULL,
    confidence double precision NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_market_observation_clocks CHECK ((available_at IS NOT NULL)),
    CONSTRAINT ck_market_observation_status CHECK ((status = ANY (ARRAY['AVAILABLE'::text, 'MISSING_SOURCE'::text, 'MISSING_HISTORY'::text, 'CONFLICTED'::text, 'FALLBACK'::text, 'UNSUPPORTED'::text, 'STALE'::text]))),
    CONSTRAINT market_observation_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision)))
);

CREATE TABLE raw.option_capture_generation (
    id bigint NOT NULL,
    snapshot_id bigint NOT NULL,
    ingest_run_id uuid NOT NULL,
    generation integer NOT NULL,
    capture_state text NOT NULL,
    expected_contract_count integer DEFAULT 0 NOT NULL,
    received_contract_count integer DEFAULT 0 NOT NULL,
    completeness double precision DEFAULT 0 NOT NULL,
    capture_started_at timestamp with time zone DEFAULT now() NOT NULL,
    capture_finished_at timestamp with time zone,
    terminal_error text,
    diagnostics jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT option_capture_generation_capture_state_check CHECK ((capture_state = ANY (ARRAY['running'::text, 'complete'::text, 'partial'::text, 'failed'::text, 'deferred'::text]))),
    CONSTRAINT option_capture_generation_completeness_check CHECK (((completeness >= (0)::double precision) AND (completeness <= (1)::double precision)))
);

ALTER TABLE raw.option_capture_generation ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME raw.option_capture_generation_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE raw.option_quote (
    id bigint NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    snapshot_id bigint NOT NULL,
    contract_id bigint NOT NULL,
    underlying_price double precision,
    bid double precision,
    ask double precision,
    mid double precision,
    last double precision,
    volume bigint,
    open_interest bigint,
    provider_iv double precision,
    provider_delta double precision,
    provider_gamma double precision,
    provider_theta double precision,
    provider_vega double precision,
    bid_size bigint,
    ask_size bigint,
    last_trade_at timestamp with time zone,
    captured_at timestamp with time zone,
    market_data_status text,
    previous_close double precision,
    provider_rho double precision,
    chance_of_profit_long double precision,
    chance_of_profit_short double precision,
    provider_updated_at timestamp with time zone,
    provider_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    capture_generation_id bigint,
    capture_group_key text,
    group_started_at timestamp with time zone,
    group_finished_at timestamp with time zone,
    provider_observed_at timestamp with time zone,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    underlying_observed_at timestamp with time zone,
    underlying_available_at timestamp with time zone,
    contract_style text,
    contract_settlement text,
    contract_deliverable_key text,
    standard_contract_verified boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_option_quote_standard_terms CHECK (((NOT standard_contract_verified) OR ((contract_style = 'american'::text) AND (contract_settlement = 'physical'::text) AND (contract_deliverable_key IS NOT NULL))))
)
PARTITION BY RANGE (observed_at);

CREATE TABLE raw.option_quote_default (
    id bigint CONSTRAINT option_quote_id_not_null NOT NULL,
    observed_at timestamp with time zone CONSTRAINT option_quote_observed_at_not_null NOT NULL,
    snapshot_id bigint CONSTRAINT option_quote_snapshot_id_not_null NOT NULL,
    contract_id bigint CONSTRAINT option_quote_contract_id_not_null NOT NULL,
    underlying_price double precision,
    bid double precision,
    ask double precision,
    mid double precision,
    last double precision,
    volume bigint,
    open_interest bigint,
    provider_iv double precision,
    provider_delta double precision,
    provider_gamma double precision,
    provider_theta double precision,
    provider_vega double precision,
    bid_size bigint,
    ask_size bigint,
    last_trade_at timestamp with time zone,
    captured_at timestamp with time zone,
    market_data_status text,
    previous_close double precision,
    provider_rho double precision,
    chance_of_profit_long double precision,
    chance_of_profit_short double precision,
    provider_updated_at timestamp with time zone,
    provider_payload jsonb DEFAULT '{}'::jsonb CONSTRAINT option_quote_provider_payload_not_null NOT NULL,
    capture_generation_id bigint,
    capture_group_key text,
    group_started_at timestamp with time zone,
    group_finished_at timestamp with time zone,
    provider_observed_at timestamp with time zone,
    available_at timestamp with time zone DEFAULT now() CONSTRAINT option_quote_available_at_not_null NOT NULL,
    underlying_observed_at timestamp with time zone,
    underlying_available_at timestamp with time zone,
    contract_style text,
    contract_settlement text,
    contract_deliverable_key text,
    standard_contract_verified boolean DEFAULT false CONSTRAINT option_quote_standard_contract_verified_not_null NOT NULL,
    CONSTRAINT ck_option_quote_standard_terms CHECK (((NOT standard_contract_verified) OR ((contract_style = 'american'::text) AND (contract_settlement = 'physical'::text) AND (contract_deliverable_key IS NOT NULL))))
);

ALTER TABLE raw.option_quote ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME raw.option_quote_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE raw.option_snapshot (
    id bigint NOT NULL,
    source_id text NOT NULL,
    ingest_run_id uuid NOT NULL,
    payload_id bigint,
    observed_at timestamp with time zone NOT NULL,
    trading_date date NOT NULL,
    market_session text NOT NULL,
    universe text NOT NULL,
    completeness double precision,
    contract_count integer DEFAULT 0 NOT NULL,
    collection_profile text DEFAULT 'radar'::text NOT NULL,
    history_symbol text,
    slot_at timestamp with time zone,
    capture_started_at timestamp with time zone,
    capture_finished_at timestamp with time zone,
    expected_contract_count integer,
    received_contract_count integer,
    capture_state text DEFAULT 'complete'::text NOT NULL,
    latest_complete_generation_id bigint,
    CONSTRAINT ck_option_snapshot_capture_state CHECK ((capture_state = ANY (ARRAY['running'::text, 'complete'::text, 'partial'::text, 'failed'::text, 'deferred'::text]))),
    CONSTRAINT ck_option_snapshot_profile CHECK ((collection_profile = ANY (ARRAY['radar'::text, 'history_full'::text, 'event_strip'::text]))),
    CONSTRAINT option_snapshot_completeness_check CHECK (((completeness >= (0)::double precision) AND (completeness <= (1)::double precision))),
    CONSTRAINT option_snapshot_market_session_check CHECK ((market_session = ANY (ARRAY['premarket'::text, 'regular'::text, 'afterhours'::text, 'closed'::text, 'unknown'::text])))
);

ALTER TABLE raw.option_snapshot ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME raw.option_snapshot_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE raw.price_bar_confirmation (
    fact_id bigint NOT NULL,
    fact_available_at timestamp with time zone NOT NULL,
    ingest_run_id uuid NOT NULL,
    confirmed_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);

ALTER TABLE raw.price_bar ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME raw.price_bar_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE raw.quote_confirmation (
    fact_id bigint NOT NULL,
    fact_available_at timestamp with time zone NOT NULL,
    ingest_run_id uuid NOT NULL,
    confirmed_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL
);

ALTER TABLE raw.quote ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME raw.quote_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

ALTER TABLE ONLY raw.option_quote ATTACH PARTITION raw.option_quote_default DEFAULT;

ALTER TABLE ONLY analysis.agent_experiment
    ADD CONSTRAINT agent_experiment_experiment_key_key UNIQUE (experiment_key);

ALTER TABLE ONLY analysis.agent_experiment
    ADD CONSTRAINT agent_experiment_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.agent_run
    ADD CONSTRAINT agent_run_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.agent_task
    ADD CONSTRAINT agent_task_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.book_attribution
    ADD CONSTRAINT book_attribution_pkey PRIMARY KEY (book_attribution_id);

ALTER TABLE ONLY analysis.decision_evidence
    ADD CONSTRAINT decision_evidence_pkey PRIMARY KEY (decision_id, evidence_kind, reference_key);

ALTER TABLE ONLY analysis.decision
    ADD CONSTRAINT decision_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.decision
    ADD CONSTRAINT decision_run_id_decision_key_key UNIQUE (run_id, decision_key);

ALTER TABLE ONLY analysis.event_decision_packet
    ADD CONSTRAINT event_decision_packet_pkey PRIMARY KEY (event_id);

ALTER TABLE ONLY analysis.event_scout_event
    ADD CONSTRAINT event_scout_event_pkey PRIMARY KEY (event_id);

ALTER TABLE ONLY analysis.event_study_feature
    ADD CONSTRAINT event_study_feature_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.event_study_feature
    ADD CONSTRAINT event_study_feature_run_id_instrument_id_market_event_versi_key UNIQUE (run_id, instrument_id, market_event_version_id, horizon, feature_version);

ALTER TABLE ONLY analysis.execution_model_snapshot
    ADD CONSTRAINT execution_model_snapshot_pkey PRIMARY KEY (execution_model_snapshot_id);

ALTER TABLE ONLY analysis.experiment_family
    ADD CONSTRAINT experiment_family_family_key_key UNIQUE (family_key);

ALTER TABLE ONLY analysis.experiment_family
    ADD CONSTRAINT experiment_family_hypothesis_id_name_key UNIQUE (hypothesis_id, name);

ALTER TABLE ONLY analysis.experiment_family
    ADD CONSTRAINT experiment_family_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.experiment_manifest
    ADD CONSTRAINT experiment_manifest_pkey PRIMARY KEY (experiment_family_id);

ALTER TABLE ONLY analysis.hypothesis
    ADD CONSTRAINT hypothesis_hypothesis_key_key UNIQUE (hypothesis_key);

ALTER TABLE ONLY analysis.hypothesis
    ADD CONSTRAINT hypothesis_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.market_coverage_vector
    ADD CONSTRAINT market_coverage_vector_pkey PRIMARY KEY (vector_id);

ALTER TABLE ONLY analysis.market_scenario_path
    ADD CONSTRAINT market_scenario_path_pkey PRIMARY KEY (scenario_hash);

ALTER TABLE ONLY analysis.market_state_posterior
    ADD CONSTRAINT market_state_posterior_pkey PRIMARY KEY (posterior_id);

ALTER TABLE ONLY analysis.option_decision
    ADD CONSTRAINT option_decision_pkey PRIMARY KEY (decision_id);

ALTER TABLE ONLY analysis.option_discovery_candidate
    ADD CONSTRAINT option_discovery_candidate_pkey PRIMARY KEY (run_id, instrument_id);

ALTER TABLE ONLY analysis.option_discovery_run
    ADD CONSTRAINT option_discovery_run_pkey PRIMARY KEY (run_id);

ALTER TABLE ONLY analysis.option_event_agent_batch
    ADD CONSTRAINT option_event_agent_batch_event_id_fingerprint_key_key UNIQUE (event_id, fingerprint_key);

ALTER TABLE ONLY analysis.option_event_agent_batch
    ADD CONSTRAINT option_event_agent_batch_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_event_capture
    ADD CONSTRAINT option_event_capture_event_id_scheduled_at_key UNIQUE (event_id, scheduled_at);

ALTER TABLE ONLY analysis.option_event_capture
    ADD CONSTRAINT option_event_capture_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_event_contract
    ADD CONSTRAINT option_event_contract_event_id_contract_key_key UNIQUE (event_id, contract_key);

ALTER TABLE ONLY analysis.option_event_contract
    ADD CONSTRAINT option_event_contract_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_event_detector_run
    ADD CONSTRAINT option_event_detector_run_cohort_id_scheduled_at_key UNIQUE (cohort_id, scheduled_at);

ALTER TABLE ONLY analysis.option_event_detector_run
    ADD CONSTRAINT option_event_detector_run_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_event
    ADD CONSTRAINT option_event_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_event_signal
    ADD CONSTRAINT option_event_signal_event_id_event_contract_id_capture_id_s_key UNIQUE (event_id, event_contract_id, capture_id, strategy_key, strategy_revision_id);

ALTER TABLE ONLY analysis.option_event_signal
    ADD CONSTRAINT option_event_signal_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_event_spot
    ADD CONSTRAINT option_event_spot_pkey PRIMARY KEY (event_id, observed_at);

ALTER TABLE ONLY analysis.option_feature
    ADD CONSTRAINT option_feature_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_feature
    ADD CONSTRAINT option_feature_run_id_snapshot_id_contract_id_feature_versi_key UNIQUE (run_id, snapshot_id, contract_id, feature_version);

ALTER TABLE ONLY analysis.option_gate_result
    ADD CONSTRAINT option_gate_result_pkey PRIMARY KEY (run_id, instrument_id, gate_code);

ALTER TABLE ONLY analysis.option_history_anomaly
    ADD CONSTRAINT option_history_anomaly_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_history_canary
    ADD CONSTRAINT option_history_canary_model_revision_started_at_key UNIQUE (model_revision, started_at);

ALTER TABLE ONLY analysis.option_history_canary
    ADD CONSTRAINT option_history_canary_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_liquidity_sla
    ADD CONSTRAINT option_liquidity_sla_pkey PRIMARY KEY (sla_id);

ALTER TABLE ONLY analysis.option_opportunity_observation
    ADD CONSTRAINT option_opportunity_observatio_event_id_capture_generation_k_key UNIQUE (event_id, capture_generation_key, contract_id, strategy_key, strategy_revision_id);

ALTER TABLE ONLY analysis.option_opportunity_observation
    ADD CONSTRAINT option_opportunity_observation_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_outcome
    ADD CONSTRAINT option_outcome_pkey PRIMARY KEY (decision_id);

ALTER TABLE ONLY analysis.option_recovery_cohort
    ADD CONSTRAINT option_recovery_cohort_objective_version_code_version_key UNIQUE (objective_version, code_version);

ALTER TABLE ONLY analysis.option_recovery_cohort
    ADD CONSTRAINT option_recovery_cohort_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_recovery_event_session_quality
    ADD CONSTRAINT option_recovery_event_session_quality_event_id_trading_date_key UNIQUE (event_id, trading_date);

ALTER TABLE ONLY analysis.option_recovery_event_session_quality
    ADD CONSTRAINT option_recovery_event_session_quality_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_recovery_program_session
    ADD CONSTRAINT option_recovery_program_session_cohort_id_trading_date_key UNIQUE (cohort_id, trading_date);

ALTER TABLE ONLY analysis.option_recovery_program_session
    ADD CONSTRAINT option_recovery_program_session_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_relative_value
    ADD CONSTRAINT option_relative_value_analysis_run_id_capture_generation_id_key UNIQUE (analysis_run_id, capture_generation_id, contract_id, model_revision);

ALTER TABLE ONLY analysis.option_relative_value
    ADD CONSTRAINT option_relative_value_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_relative_value_verification
    ADD CONSTRAINT option_relative_value_verification_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_surface_shift
    ADD CONSTRAINT option_surface_shift_current_analysis_run_id_previous_analy_key UNIQUE (current_analysis_run_id, previous_analysis_run_id, feature_version);

ALTER TABLE ONLY analysis.option_surface_shift
    ADD CONSTRAINT option_surface_shift_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.option_surface_summary
    ADD CONSTRAINT option_surface_summary_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.phase4_allocation_signing_secret
    ADD CONSTRAINT phase4_allocation_signing_secret_pkey PRIMARY KEY (singleton);

ALTER TABLE ONLY analysis.portfolio_allocation_item
    ADD CONSTRAINT portfolio_allocation_item_pkey PRIMARY KEY (allocation_item_id);

ALTER TABLE ONLY analysis.portfolio_allocation_snapshot
    ADD CONSTRAINT portfolio_allocation_snapshot_pkey PRIMARY KEY (allocation_id);

ALTER TABLE ONLY analysis.portfolio_drift_evidence
    ADD CONSTRAINT portfolio_drift_evidence_pkey PRIMARY KEY (decision_id);

ALTER TABLE ONLY analysis.probabilistic_portfolio_scenario_artifact
    ADD CONSTRAINT probabilistic_portfolio_scenario_artifact_pkey PRIMARY KEY (scenario_artifact_id);

ALTER TABLE ONLY analysis.reject_summary
    ADD CONSTRAINT reject_summary_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.reject_summary
    ADD CONSTRAINT reject_summary_run_id_strategy_revision_id_instrument_id_ga_key UNIQUE (run_id, strategy_revision_id, instrument_id, gate_code);

ALTER TABLE ONLY analysis.research_evaluator_output
    ADD CONSTRAINT research_evaluator_output_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.research_evaluator_output
    ADD CONSTRAINT research_evaluator_output_trial_result_id_evidence_kind_key UNIQUE (trial_result_id, evidence_kind);

ALTER TABLE ONLY analysis.research_evaluator_signing_secret
    ADD CONSTRAINT research_evaluator_signing_secret_pkey PRIMARY KEY (singleton);

ALTER TABLE ONLY analysis.research_evidence_manifest
    ADD CONSTRAINT research_evidence_manifest_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.research_evidence_manifest
    ADD CONSTRAINT research_evidence_manifest_trial_result_id_evidence_kind_key UNIQUE (trial_result_id, evidence_kind);

ALTER TABLE ONLY analysis.research_trial
    ADD CONSTRAINT research_trial_experiment_family_id_trial_key_key UNIQUE (experiment_family_id, trial_key);

ALTER TABLE ONLY analysis.research_trial
    ADD CONSTRAINT research_trial_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.run
    ADD CONSTRAINT run_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.shadow_trade
    ADD CONSTRAINT shadow_trade_decision_id_key UNIQUE (decision_id);

ALTER TABLE ONLY analysis.shadow_trade
    ADD CONSTRAINT shadow_trade_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.source_signal
    ADD CONSTRAINT source_signal_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.source_signal
    ADD CONSTRAINT source_signal_run_id_content_item_id_instrument_id_signal_t_key UNIQUE (run_id, content_item_id, instrument_id, signal_type);

ALTER TABLE ONLY analysis.strategy_comparison
    ADD CONSTRAINT strategy_comparison_champion_revision_id_challenger_revisio_key UNIQUE (champion_revision_id, challenger_revision_id, input_cutoff, input_hash);

ALTER TABLE ONLY analysis.strategy_comparison
    ADD CONSTRAINT strategy_comparison_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.strategy_evaluation
    ADD CONSTRAINT strategy_evaluation_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.strategy_forecast
    ADD CONSTRAINT strategy_forecast_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.strategy_manifest
    ADD CONSTRAINT strategy_manifest_pkey PRIMARY KEY (strategy_revision_id);

ALTER TABLE ONLY analysis.strategy_monitoring_evidence
    ADD CONSTRAINT strategy_monitoring_evidence_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.strategy_monitoring_evidence
    ADD CONSTRAINT strategy_monitoring_evidence_strategy_revision_id_evidence__key UNIQUE (strategy_revision_id, evidence_kind, input_cutoff, input_hash);

ALTER TABLE ONLY analysis.strategy_pnl_tape
    ADD CONSTRAINT strategy_pnl_tape_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.strategy_pnl_tape
    ADD CONSTRAINT strategy_pnl_tape_strategy_forecast_id_key UNIQUE (strategy_forecast_id);

ALTER TABLE ONLY analysis.strategy_pnl_tape
    ADD CONSTRAINT strategy_pnl_tape_strategy_revision_id_instrument_id_pnl_da_key UNIQUE (strategy_revision_id, instrument_id, pnl_date, input_hash);

ALTER TABLE ONLY analysis.strategy_revision
    ADD CONSTRAINT strategy_revision_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.strategy_revision
    ADD CONSTRAINT strategy_revision_strategy_key_revision_key UNIQUE (strategy_key, revision);

ALTER TABLE ONLY analysis.symbol_decision_outcome
    ADD CONSTRAINT symbol_decision_outcome_pkey PRIMARY KEY (decision_id);

ALTER TABLE ONLY analysis.symbol_decision
    ADD CONSTRAINT symbol_decision_pkey PRIMARY KEY (decision_id);

ALTER TABLE ONLY analysis.symbol_feature
    ADD CONSTRAINT symbol_feature_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.symbol_feature
    ADD CONSTRAINT symbol_feature_run_id_instrument_id_feature_set_feature_ver_key UNIQUE (run_id, instrument_id, feature_set, feature_version);

ALTER TABLE ONLY analysis.ticker_benchmark_snapshot
    ADD CONSTRAINT ticker_benchmark_snapshot_benchmark_key_as_of_key UNIQUE (benchmark_key, as_of);

ALTER TABLE ONLY analysis.ticker_benchmark_snapshot
    ADD CONSTRAINT ticker_benchmark_snapshot_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.ticker_data_request
    ADD CONSTRAINT ticker_data_request_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.ticker_data_request
    ADD CONSTRAINT ticker_data_request_ticker_decision_id_field_key UNIQUE (ticker_decision_id, field);

ALTER TABLE ONLY analysis.ticker_decision
    ADD CONSTRAINT ticker_decision_instrument_id_decision_revision_key UNIQUE (instrument_id, decision_revision);

ALTER TABLE ONLY analysis.ticker_decision
    ADD CONSTRAINT ticker_decision_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.ticker_input_manifest
    ADD CONSTRAINT ticker_input_manifest_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.ticker_input_manifest
    ADD CONSTRAINT ticker_input_manifest_ticker_decision_id_field_source_id_av_key UNIQUE (ticker_decision_id, field, source_id, available_at, revision);

ALTER TABLE ONLY analysis.ticker_outcome
    ADD CONSTRAINT ticker_outcome_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.ticker_outcome
    ADD CONSTRAINT ticker_outcome_ticker_decision_id_horizon_horizon_sessions_key UNIQUE (ticker_decision_id, horizon, horizon_sessions);

ALTER TABLE ONLY analysis.trial_result
    ADD CONSTRAINT trial_result_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.trial_result
    ADD CONSTRAINT trial_result_research_trial_id_result_kind_result_version_key UNIQUE (research_trial_id, result_kind, result_version);

ALTER TABLE ONLY analysis.trial_universe_manifest
    ADD CONSTRAINT trial_universe_manifest_pkey PRIMARY KEY (research_trial_id);

ALTER TABLE ONLY analysis.universe_observation
    ADD CONSTRAINT universe_observation_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.universe_observation
    ADD CONSTRAINT universe_observation_research_trial_id_cutoff_instrument_id_key UNIQUE (research_trial_id, cutoff, instrument_id);

ALTER TABLE ONLY analysis.validation_dossier
    ADD CONSTRAINT validation_dossier_pkey PRIMARY KEY (id);

ALTER TABLE ONLY analysis.validation_dossier
    ADD CONSTRAINT validation_dossier_strategy_revision_id_key UNIQUE (strategy_revision_id);

ALTER TABLE ONLY analysis.validation_gate_result
    ADD CONSTRAINT validation_gate_result_dossier_id_gate_code_key UNIQUE (dossier_id, gate_code);

ALTER TABLE ONLY analysis.validation_gate_result
    ADD CONSTRAINT validation_gate_result_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.alert
    ADD CONSTRAINT alert_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.catalyst
    ADD CONSTRAINT catalyst_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.current_publication_item
    ADD CONSTRAINT current_publication_item_pkey PRIMARY KEY (scope, model_name, stable_key);

ALTER TABLE ONLY app.decision_inbox_item
    ADD CONSTRAINT decision_inbox_item_dedupe_key_key UNIQUE (dedupe_key);

ALTER TABLE ONLY app.decision_inbox_item
    ADD CONSTRAINT decision_inbox_item_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.decision_inbox_sync_state
    ADD CONSTRAINT decision_inbox_sync_state_pkey PRIMARY KEY (state_key);

ALTER TABLE ONLY app.decision_truth
    ADD CONSTRAINT decision_truth_pkey PRIMARY KEY (symbol, lane);

ALTER TABLE ONLY app.manual_account_snapshot
    ADD CONSTRAINT manual_account_snapshot_idempotency_key_key UNIQUE (idempotency_key);

ALTER TABLE ONLY app.manual_account_snapshot
    ADD CONSTRAINT manual_account_snapshot_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.notification_outbox
    ADD CONSTRAINT notification_outbox_dedupe_key_key UNIQUE (dedupe_key);

ALTER TABLE ONLY app.notification_outbox
    ADD CONSTRAINT notification_outbox_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.option_history_policy
    ADD CONSTRAINT option_history_policy_pkey PRIMARY KEY (instrument_id, profile);

ALTER TABLE ONLY app.paper_execution_observation
    ADD CONSTRAINT paper_execution_observation_pkey PRIMARY KEY (paper_execution_observation_id);

ALTER TABLE ONLY app.paper_order_leg
    ADD CONSTRAINT paper_order_leg_pkey PRIMARY KEY (paper_order_id, leg_index);

ALTER TABLE ONLY app.paper_order
    ADD CONSTRAINT paper_order_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.portfolio_position
    ADD CONSTRAINT portfolio_position_pkey PRIMARY KEY (instrument_id);

ALTER TABLE ONLY app.portfolio_transaction
    ADD CONSTRAINT portfolio_transaction_idempotency_key_key UNIQUE (idempotency_key);

ALTER TABLE ONLY app.portfolio_transaction
    ADD CONSTRAINT portfolio_transaction_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.publication_bundle_item
    ADD CONSTRAINT publication_bundle_item_pkey PRIMARY KEY (bundle_id, model_name, stable_key);

ALTER TABLE ONLY app.publication_bundle
    ADD CONSTRAINT publication_bundle_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.publication_bundle
    ADD CONSTRAINT publication_bundle_scope_bundle_hash_key UNIQUE (scope, bundle_hash);

ALTER TABLE ONLY app.publication_item
    ADD CONSTRAINT publication_item_pkey PRIMARY KEY (publication_id, model_name, stable_key);

ALTER TABLE ONLY app.publication_payload
    ADD CONSTRAINT publication_payload_pkey PRIMARY KEY (content_hash);

ALTER TABLE ONLY app.publication
    ADD CONSTRAINT publication_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.research_report
    ADD CONSTRAINT research_report_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.setting
    ADD CONSTRAINT setting_pkey PRIMARY KEY (key);

ALTER TABLE ONLY app.thesis_automation_run
    ADD CONSTRAINT thesis_automation_run_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.thesis_evidence_assessment
    ADD CONSTRAINT thesis_evidence_assessment_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.thesis_expression
    ADD CONSTRAINT thesis_expression_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.thesis
    ADD CONSTRAINT thesis_instrument_id_revision_key UNIQUE (instrument_id, revision);

ALTER TABLE ONLY app.thesis
    ADD CONSTRAINT thesis_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.thesis_review_event
    ADD CONSTRAINT thesis_review_event_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.trade_journal
    ADD CONSTRAINT trade_journal_pkey PRIMARY KEY (id);

ALTER TABLE ONLY app.paper_order
    ADD CONSTRAINT uq_app_paper_order_idempotency UNIQUE (idempotency_key);

ALTER TABLE ONLY app.watchlist_item
    ADD CONSTRAINT watchlist_item_pkey PRIMARY KEY (instrument_id);

ALTER TABLE ONLY catalog.instrument_alias
    ADD CONSTRAINT instrument_alias_pkey PRIMARY KEY (id);

ALTER TABLE ONLY catalog.instrument_alias
    ADD CONSTRAINT instrument_alias_provider_external_symbol_exchange_key UNIQUE (provider, external_symbol, exchange);

ALTER TABLE ONLY catalog.instrument
    ADD CONSTRAINT instrument_pkey PRIMARY KEY (id);

ALTER TABLE ONLY catalog.instrument
    ADD CONSTRAINT instrument_symbol_key UNIQUE (symbol);

ALTER TABLE ONLY catalog.option_contract
    ADD CONSTRAINT option_contract_pkey PRIMARY KEY (id);

ALTER TABLE ONLY catalog.option_contract
    ADD CONSTRAINT uq_option_contract_deliverable UNIQUE (underlying_instrument_id, expiration, strike, option_type, multiplier, deliverable_key);

ALTER TABLE ONLY ingest.payload
    ADD CONSTRAINT payload_pkey PRIMARY KEY (id);

ALTER TABLE ONLY ingest.payload
    ADD CONSTRAINT payload_sha256_key UNIQUE (sha256);

ALTER TABLE ONLY ingest.run
    ADD CONSTRAINT run_pkey PRIMARY KEY (id);

ALTER TABLE ONLY ingest.run
    ADD CONSTRAINT run_source_id_source_run_key_key UNIQUE (source_id, source_run_key);

ALTER TABLE ONLY ingest.source_lifecycle_history
    ADD CONSTRAINT source_lifecycle_history_pkey PRIMARY KEY (id);

ALTER TABLE ONLY ingest.source_lifecycle_history
    ADD CONSTRAINT source_lifecycle_history_source_id_effective_at_enabled_ope_key UNIQUE (source_id, effective_at, enabled, operational_state);

ALTER TABLE ONLY ingest.source
    ADD CONSTRAINT source_pkey PRIMARY KEY (id);

ALTER TABLE ONLY ops.job_run
    ADD CONSTRAINT job_run_pkey PRIMARY KEY (id);

ALTER TABLE ONLY ops.option_quote_partition_policy
    ADD CONSTRAINT option_quote_partition_policy_pkey PRIMARY KEY (policy_key);

ALTER TABLE ONLY ops.provider_lease
    ADD CONSTRAINT provider_lease_pkey PRIMARY KEY (id);

ALTER TABLE ONLY ops.storage_archive_checkpoint
    ADD CONSTRAINT storage_archive_checkpoint_pkey PRIMARY KEY (checkpoint_key);

ALTER TABLE ONLY ops.storage_archive_manifest
    ADD CONSTRAINT storage_archive_manifest_archive_kind_sha256_key UNIQUE (archive_kind, sha256);

ALTER TABLE ONLY ops.storage_archive_manifest
    ADD CONSTRAINT storage_archive_manifest_nas_uri_key UNIQUE (nas_uri);

ALTER TABLE ONLY ops.storage_archive_manifest
    ADD CONSTRAINT storage_archive_manifest_pkey PRIMARY KEY (id);

ALTER TABLE ONLY ops.storage_archive_manifest_reference
    ADD CONSTRAINT storage_archive_manifest_refe_source_relation_source_row_id_key UNIQUE (source_relation, source_row_id);

ALTER TABLE ONLY ops.storage_archive_manifest_reference
    ADD CONSTRAINT storage_archive_manifest_reference_pkey PRIMARY KEY (manifest_id, source_relation, source_row_id);

ALTER TABLE ONLY raw.broker_account_snapshot
    ADD CONSTRAINT broker_account_snapshot_pkey PRIMARY KEY (id);

ALTER TABLE ONLY raw.broker_account_snapshot
    ADD CONSTRAINT broker_account_snapshot_source_id_account_key_observed_at_key UNIQUE (source_id, account_key, observed_at);

ALTER TABLE ONLY raw.broker_activity
    ADD CONSTRAINT broker_activity_pkey PRIMARY KEY (id);

ALTER TABLE ONLY raw.broker_activity
    ADD CONSTRAINT broker_activity_source_id_activity_key_activity_type_key UNIQUE (source_id, activity_key, activity_type);

ALTER TABLE ONLY raw.broker_position_snapshot
    ADD CONSTRAINT broker_position_snapshot_account_snapshot_id_instrument_id_key UNIQUE (account_snapshot_id, instrument_id);

ALTER TABLE ONLY raw.broker_position_snapshot
    ADD CONSTRAINT broker_position_snapshot_pkey PRIMARY KEY (id);

ALTER TABLE ONLY raw.content_item_instrument
    ADD CONSTRAINT content_item_instrument_pkey PRIMARY KEY (content_item_id, instrument_id);

ALTER TABLE ONLY raw.content_item
    ADD CONSTRAINT content_item_pkey PRIMARY KEY (id);

ALTER TABLE ONLY raw.content_item
    ADD CONSTRAINT content_item_source_id_source_key_key UNIQUE (source_id, source_key);

ALTER TABLE ONLY raw.disclosure
    ADD CONSTRAINT disclosure_pkey PRIMARY KEY (id);

ALTER TABLE ONLY raw.disclosure
    ADD CONSTRAINT disclosure_source_id_source_key_key UNIQUE (source_id, source_key);

ALTER TABLE ONLY raw.fundamental_observation
    ADD CONSTRAINT fundamental_observation_pkey PRIMARY KEY (id);

ALTER TABLE ONLY raw.market_event
    ADD CONSTRAINT market_event_pkey PRIMARY KEY (id);

ALTER TABLE ONLY raw.market_event
    ADD CONSTRAINT market_event_source_id_source_key_key UNIQUE (source_id, source_key);

ALTER TABLE ONLY raw.market_event_version
    ADD CONSTRAINT market_event_version_market_event_id_ingest_run_id_key UNIQUE (market_event_id, ingest_run_id);

ALTER TABLE ONLY raw.market_event_version
    ADD CONSTRAINT market_event_version_pkey PRIMARY KEY (id);

ALTER TABLE ONLY raw.market_observation
    ADD CONSTRAINT market_observation_pkey PRIMARY KEY (observation_id);

ALTER TABLE ONLY raw.option_capture_generation
    ADD CONSTRAINT option_capture_generation_pkey PRIMARY KEY (id);

ALTER TABLE ONLY raw.option_capture_generation
    ADD CONSTRAINT option_capture_generation_snapshot_id_generation_key UNIQUE (snapshot_id, generation);

ALTER TABLE ONLY raw.option_capture_generation
    ADD CONSTRAINT option_capture_generation_snapshot_id_ingest_run_id_key UNIQUE (snapshot_id, ingest_run_id);

ALTER TABLE ONLY raw.option_quote
    ADD CONSTRAINT option_quote_snapshot_id_contract_id_observed_at_key UNIQUE (snapshot_id, contract_id, observed_at);

ALTER TABLE ONLY raw.option_quote_default
    ADD CONSTRAINT option_quote_default_snapshot_id_contract_id_observed_at_key UNIQUE (snapshot_id, contract_id, observed_at);

ALTER TABLE ONLY raw.option_snapshot
    ADD CONSTRAINT option_snapshot_pkey PRIMARY KEY (id);

ALTER TABLE ONLY raw.option_snapshot
    ADD CONSTRAINT option_snapshot_source_id_observed_at_universe_key UNIQUE (source_id, observed_at, universe);

ALTER TABLE ONLY raw.price_bar_confirmation
    ADD CONSTRAINT price_bar_confirmation_pkey PRIMARY KEY (fact_id, fact_available_at, ingest_run_id);

ALTER TABLE ONLY raw.price_bar_fact_availability
    ADD CONSTRAINT price_bar_fact_availability_pkey PRIMARY KEY (fact_id, fact_available_at);

ALTER TABLE ONLY raw.price_bar
    ADD CONSTRAINT price_bar_instrument_id_source_id_interval_observed_at_key UNIQUE (instrument_id, source_id, "interval", observed_at);

ALTER TABLE ONLY raw.price_bar
    ADD CONSTRAINT price_bar_pkey PRIMARY KEY (id);

ALTER TABLE ONLY raw.quote_confirmation
    ADD CONSTRAINT quote_confirmation_pkey PRIMARY KEY (fact_id, fact_available_at, ingest_run_id);

ALTER TABLE ONLY raw.quote_fact_availability
    ADD CONSTRAINT quote_fact_availability_pkey PRIMARY KEY (fact_id, fact_available_at);

ALTER TABLE ONLY raw.quote
    ADD CONSTRAINT quote_instrument_id_source_id_observed_at_key UNIQUE (instrument_id, source_id, observed_at);

ALTER TABLE ONLY raw.quote
    ADD CONSTRAINT quote_pkey PRIMARY KEY (id);

CREATE INDEX ix_agent_run_experiment_arm_started ON analysis.agent_run USING btree (experiment_id, arm, started_at DESC) WHERE (experiment_id IS NOT NULL);

CREATE INDEX ix_agent_task_experiment_arm_created ON analysis.agent_task USING btree (experiment_id, arm, created_at DESC) WHERE (experiment_id IS NOT NULL);

CREATE INDEX ix_analysis_agent_task_decision_id ON analysis.agent_task USING btree (decision_id);

CREATE INDEX ix_analysis_decision_actionable ON analysis.decision USING btree (run_id, kind, score DESC) WHERE (state <> 'REJECT'::text);

CREATE INDEX ix_analysis_decision_instrument ON analysis.decision USING btree (instrument_id, as_of DESC);

CREATE INDEX ix_analysis_decision_recent_page ON analysis.decision USING btree (as_of DESC, id DESC);

CREATE INDEX ix_analysis_decision_run ON analysis.decision USING btree (run_id, kind, state, rank);

CREATE INDEX ix_analysis_decision_scorecard_episode ON analysis.decision USING btree (lane, episode_key, as_of DESC, id DESC) INCLUDE (run_id, state, sample_eligible, quarantine_reason, calibration_cohort) WHERE ((kind = 'option'::text) AND (calibration_cohort ~~ 'option-scorecard-truth-v1:%'::text));

CREATE INDEX ix_analysis_option_anomaly_snapshot ON analysis.option_history_anomaly USING btree (snapshot_id, state, anomaly_type);

CREATE INDEX ix_analysis_option_decision_contract_predecessor ON analysis.option_decision USING btree (contract_id, decision_id);

CREATE INDEX ix_analysis_option_decision_primary_decision_id ON analysis.option_decision USING btree (primary_decision_id);

CREATE INDEX ix_analysis_option_decision_relative_value_id ON analysis.option_decision USING btree (relative_value_id);

CREATE INDEX ix_analysis_option_decision_structure ON analysis.option_decision USING btree (structure);

CREATE INDEX ix_analysis_option_event_signal_decision_id ON analysis.option_event_signal USING btree (decision_id);

CREATE INDEX ix_analysis_option_feature_lookup ON analysis.option_feature USING btree (snapshot_id, contract_id, feature_version);

CREATE INDEX ix_analysis_option_surface_history ON analysis.option_surface_summary USING btree (snapshot_id, expiration, option_type);

CREATE INDEX ix_analysis_outcome_maturity ON analysis.option_outcome USING btree (maturity_state, observed_through);

CREATE INDEX ix_analysis_source_signal_instrument ON analysis.source_signal USING btree (instrument_id, observed_at DESC);

CREATE INDEX ix_analysis_source_signal_pit ON analysis.source_signal USING btree (instrument_id, available_at DESC, observed_at DESC);

CREATE INDEX ix_analysis_symbol_feature_lookup ON analysis.symbol_feature USING btree (instrument_id, as_of DESC, feature_set);

CREATE INDEX ix_decision_lane_episode_asof ON analysis.decision USING btree (lane, episode_key, as_of DESC) WHERE (kind = 'option'::text);

CREATE INDEX ix_event_decision_packet_symbol_as_of ON analysis.event_decision_packet USING btree (symbol, as_of DESC);

CREATE INDEX ix_event_scout_event_symbol_observed ON analysis.event_scout_event USING btree (symbol, observed_at DESC);

CREATE INDEX ix_event_study_feature_lookup ON analysis.event_study_feature USING btree (instrument_id, event_kind, as_of DESC);

CREATE INDEX ix_experiment_family_hypothesis ON analysis.experiment_family USING btree (hypothesis_id, id);

CREATE INDEX ix_hypothesis_available ON analysis.hypothesis USING btree (available_at, id);

CREATE INDEX ix_market_coverage_vector_as_of ON analysis.market_coverage_vector USING btree (as_of DESC);

CREATE INDEX ix_market_scenario_path_snapshot ON analysis.market_scenario_path USING btree (snapshot_id);

CREATE INDEX ix_market_state_posterior_as_of ON analysis.market_state_posterior USING btree (as_of DESC);

CREATE INDEX ix_option_event_agent_batch_cohort_queue ON analysis.option_event_agent_batch USING btree (cohort_id, status, created_at);

CREATE INDEX ix_option_event_agent_batch_event_day ON analysis.option_event_agent_batch USING btree (event_id, created_at DESC);

CREATE INDEX ix_option_event_agent_batch_experiment ON analysis.option_event_agent_batch USING btree (experiment_id, arm, created_at DESC) WHERE (experiment_id IS NOT NULL);

CREATE INDEX ix_option_event_agent_batch_queue ON analysis.option_event_agent_batch USING btree (status, created_at, event_id);

CREATE INDEX ix_option_event_capture_event_slot ON analysis.option_event_capture USING btree (event_id, scheduled_at DESC);

CREATE INDEX ix_option_event_cohort_status_detected ON analysis.option_event USING btree (cohort_id, status, detected_at DESC);

CREATE INDEX ix_option_event_contract_event_initial ON analysis.option_event_contract USING btree (event_id, is_initial, retired_at);

CREATE INDEX ix_option_event_detector_run_cohort_time ON analysis.option_event_detector_run USING btree (cohort_id, scheduled_at DESC);

CREATE INDEX ix_option_event_signal_cohort_status ON analysis.option_event_signal USING btree (cohort_id, status, available_at DESC);

CREATE INDEX ix_option_event_signal_contract_family ON analysis.option_event_signal USING btree (contract_id, strategy_key, available_at DESC);

CREATE INDEX ix_option_event_signal_event_status ON analysis.option_event_signal USING btree (event_id, status, available_at DESC);

CREATE INDEX ix_option_event_spot_event_available ON analysis.option_event_spot USING btree (event_id, available_at DESC);

CREATE INDEX ix_option_event_status_detected ON analysis.option_event USING btree (status, detected_at DESC);

CREATE INDEX ix_option_liquidity_sla_as_of ON analysis.option_liquidity_sla USING btree (as_of DESC);

CREATE INDEX ix_option_opportunity_event_selection ON analysis.option_opportunity_observation USING btree (event_id, strategy_key, selection_stage, available_at DESC);

CREATE INDEX ix_option_opportunity_lane_episode ON analysis.option_opportunity_observation USING btree (lane, episode_key, available_at DESC);

CREATE INDEX ix_option_opportunity_measurement ON analysis.option_opportunity_observation USING btree (strategy_key, outcome_classification, measured_through DESC);

CREATE INDEX ix_option_opportunity_observation_cohort_measurement ON analysis.option_opportunity_observation USING btree (cohort_id, strategy_key, outcome_classification, measured_through DESC);

CREATE INDEX ix_option_opportunity_signal ON analysis.option_opportunity_observation USING btree (signal_id, paper_order_id) WHERE ((signal_id IS NOT NULL) OR (paper_order_id IS NOT NULL));

CREATE INDEX ix_option_outcome_lane_episode_eligible ON analysis.option_outcome USING btree (lane, episode_key, sample_eligible, observed_through DESC);

CREATE INDEX ix_option_outcome_objective_classification ON analysis.option_outcome USING btree (objective_version, outcome_classification, promotion_eligible);

CREATE INDEX ix_option_outcome_source_shadow ON analysis.option_outcome USING btree (outcome_source, shadow_trade_id);

CREATE INDEX ix_option_recovery_event_session_cohort_date ON analysis.option_recovery_event_session_quality USING btree (cohort_id, trading_date DESC);

CREATE INDEX ix_option_recovery_program_session_cohort_date ON analysis.option_recovery_program_session USING btree (cohort_id, trading_date DESC);

CREATE INDEX ix_option_relative_value_generation_contract ON analysis.option_relative_value USING btree (capture_generation_id, contract_id, id DESC);

CREATE INDEX ix_option_relative_value_run_classification_edge ON analysis.option_relative_value USING btree (analysis_run_id, classification, modeled_net_edge DESC);

CREATE INDEX ix_option_relative_value_verification_candidate ON analysis.option_relative_value_verification USING btree (relative_value_id, verified_at DESC);

CREATE INDEX ix_option_surface_shift_lookup ON analysis.option_surface_shift USING btree (instrument_id, as_of DESC);

CREATE INDEX ix_portfolio_allocation_item_snapshot ON analysis.portfolio_allocation_item USING btree (allocation_id, disposition, target_weight DESC);

CREATE INDEX ix_research_evaluator_output_trial ON analysis.research_evaluator_output USING btree (research_trial_id, trial_result_id, evidence_kind);

CREATE INDEX ix_research_trial_family_cutoff ON analysis.research_trial USING btree (experiment_family_id, input_cutoff, id);

CREATE INDEX ix_strategy_forecast_revision_cutoff ON analysis.strategy_forecast USING btree (strategy_revision_id, input_cutoff, instrument_id);

CREATE INDEX ix_strategy_monitoring_revision_kind ON analysis.strategy_monitoring_evidence USING btree (strategy_revision_id, evidence_kind, input_cutoff DESC);

CREATE INDEX ix_strategy_pnl_tape_date ON analysis.strategy_pnl_tape USING btree (strategy_revision_id, pnl_date, instrument_id);

CREATE INDEX ix_symbol_decision_outcome_state_measured ON analysis.symbol_decision_outcome USING btree (state, sample_eligible, measured_through DESC);

CREATE INDEX ix_ticker_benchmark_latest ON analysis.ticker_benchmark_snapshot USING btree (benchmark_key, as_of DESC);

CREATE INDEX ix_ticker_data_request_status ON analysis.ticker_data_request USING btree (status, created_at DESC);

CREATE INDEX ix_ticker_decision_latest ON analysis.ticker_decision USING btree (instrument_id, as_of DESC, created_at DESC);

CREATE INDEX ix_ticker_decision_market_publication ON analysis.ticker_decision USING btree (market_state_publication_id) WHERE (market_state_publication_id IS NOT NULL);

CREATE INDEX ix_ticker_decision_opportunity_episode ON analysis.ticker_decision USING btree (opportunity_episode_id, opportunity_cutoff DESC);

CREATE INDEX ix_ticker_decision_revision ON analysis.ticker_decision USING btree (input_hash, code_version, experiment_id);

CREATE INDEX ix_ticker_input_manifest_pit ON analysis.ticker_input_manifest USING btree (field, available_at, ticker_decision_id);

CREATE INDEX ix_ticker_outcome_learning ON analysis.ticker_outcome USING btree (horizon, state, horizon_sessions, measured_through);

CREATE INDEX ix_universe_observation_trial_cutoff ON analysis.universe_observation USING btree (research_trial_id, cutoff, rank, instrument_id);

CREATE UNIQUE INDEX uq_analysis_strategy_active_authority ON analysis.strategy_revision USING btree (authority_group) WHERE (status = 'active'::text);

CREATE UNIQUE INDEX uq_option_event_active_ladder_slot ON analysis.option_event_contract USING btree (event_id, ladder_slot_key) WHERE (retired_at IS NULL);

CREATE UNIQUE INDEX uq_option_event_open_symbol ON analysis.option_event USING btree (instrument_id) WHERE (status = ANY (ARRAY['active'::text, 'deferred_capacity'::text]));

CREATE UNIQUE INDEX uq_option_recovery_current_cohort_objective ON analysis.option_recovery_cohort USING btree (objective_version) WHERE (status = ANY (ARRAY['collecting'::text, 'qualified'::text]));

CREATE UNIQUE INDEX uq_strategy_forecast_content ON analysis.strategy_forecast USING btree (strategy_revision_id, instrument_id, opportunity_episode_id, horizon, input_cutoff, artifact_hash);

CREATE UNIQUE INDEX uq_strategy_manifest_revision_hash ON analysis.strategy_manifest USING btree (strategy_revision_id, manifest_hash);

CREATE UNIQUE INDEX ux_option_surface_summary_legacy_group ON analysis.option_surface_summary USING btree (snapshot_id, expiration, option_type, feature_version) WHERE (analysis_run_id IS NULL);

CREATE UNIQUE INDEX ux_option_surface_summary_v3_run_group ON analysis.option_surface_summary USING btree (analysis_run_id, expiration, option_type) WHERE (analysis_run_id IS NOT NULL);

CREATE INDEX ix_app_alert_decision_id ON app.alert USING btree (decision_id);

CREATE INDEX ix_app_catalyst_current_starts_at ON app.catalyst USING btree (starts_at) WHERE (status = 'current'::text);

CREATE INDEX ix_app_current_publication_item_rank ON app.current_publication_item USING btree (scope, model_name, rank);

CREATE INDEX ix_app_paper_order_decision_id ON app.paper_order USING btree (decision_id);

CREATE INDEX ix_app_paper_order_generic_execution ON app.paper_order USING btree (lane, status, created_at, id) WHERE (event_id IS NULL);

CREATE INDEX ix_app_paper_order_lane_status_created ON app.paper_order USING btree (lane, status, created_at DESC);

CREATE INDEX ix_app_paper_order_ticker_revision ON app.paper_order USING btree (ticker_decision_revision, expression_kind, status, created_at DESC);

CREATE INDEX ix_app_portfolio_transaction_instrument_time ON app.portfolio_transaction USING btree (instrument_id, executed_at, created_at);

CREATE INDEX ix_app_portfolio_transaction_recent ON app.portfolio_transaction USING btree (executed_at DESC);

CREATE INDEX ix_app_publication_analysis_run_id ON app.publication USING btree (analysis_run_id);

CREATE INDEX ix_app_publication_bundle ON app.publication USING btree (bundle_id) WHERE (bundle_id IS NOT NULL);

CREATE INDEX ix_app_publication_bundle_item_rank ON app.publication_bundle_item USING btree (bundle_id, model_name, rank);

CREATE INDEX ix_app_publication_item_rank ON app.publication_item USING btree (publication_id, model_name, rank);

CREATE INDEX ix_app_publication_latest ON app.publication USING btree (scope, published_at DESC) WHERE (status = 'published'::text);

CREATE INDEX ix_app_publication_scope_status_created ON app.publication USING btree (scope, status, published_at DESC, created_at DESC) INCLUDE (id, analysis_run_id);

CREATE INDEX ix_app_thesis_assessment_revision ON app.thesis_evidence_assessment USING btree (thesis_revision_id, stance, materiality);

CREATE INDEX ix_app_thesis_automation_run_symbol ON app.thesis_automation_run USING btree (instrument_id, started_at DESC);

CREATE INDEX ix_app_thesis_expression_revision ON app.thesis_expression USING btree (thesis_revision_id, expression_kind);

CREATE INDEX ix_app_thesis_instrument_revision ON app.thesis USING btree (instrument_id, revision DESC);

CREATE INDEX ix_app_thesis_review_event_symbol ON app.thesis_review_event USING btree (instrument_id, created_at DESC);

CREATE INDEX ix_app_trade_journal_decision_id ON app.trade_journal USING btree (decision_id);

CREATE INDEX ix_decision_inbox_active_created ON app.decision_inbox_item USING btree (status, created_at DESC, id DESC);

CREATE INDEX ix_decision_inbox_opportunity ON app.decision_inbox_item USING btree (opportunity_id, ticket_version, event_type);

CREATE INDEX ix_decision_inbox_user_state ON app.decision_inbox_item USING btree (user_state, snoozed_until, created_at DESC);

CREATE INDEX ix_decision_truth_as_of ON app.decision_truth USING btree (as_of DESC, symbol);

CREATE INDEX ix_manual_account_snapshot_effective ON app.manual_account_snapshot USING btree (account_key, effective_at DESC, id DESC);

CREATE INDEX ix_notification_outbox_due ON app.notification_outbox USING btree (status, next_attempt_at, created_at) WHERE (status = ANY (ARRAY['queued'::text, 'failed'::text]));

CREATE INDEX ix_option_history_policy_event_active ON app.option_history_policy USING btree (profile, event_id, effective_state, expires_at);

CREATE INDEX ix_option_history_policy_state ON app.option_history_policy USING btree (requested_state, effective_state, collection_tier);

CREATE INDEX ix_recovery_paper_order_cohort ON app.paper_order USING btree (cohort_id, status, created_at DESC) WHERE (cohort_id IS NOT NULL);

CREATE INDEX ix_recovery_paper_order_signal ON app.paper_order USING btree (event_signal_id, status, created_at DESC) WHERE (event_signal_id IS NOT NULL);

CREATE UNIQUE INDEX uq_app_catalyst_current_event_key ON app.catalyst USING btree (event_key) WHERE (status = 'current'::text);

CREATE UNIQUE INDEX uq_app_publication_one_published_scope ON app.publication USING btree (scope) WHERE (status = 'published'::text);

CREATE UNIQUE INDEX uq_app_thesis_current ON app.thesis USING btree (instrument_id) WHERE (status = 'current'::text);

CREATE UNIQUE INDEX uq_app_thesis_expression_active_kind ON app.thesis_expression USING btree (thesis_revision_id, expression_kind) WHERE (status = 'active'::text);

CREATE UNIQUE INDEX uq_recovery_paper_order_event_family ON app.paper_order USING btree (event_id, strategy_family) WHERE ((event_id IS NOT NULL) AND (strategy_family IS NOT NULL));

CREATE UNIQUE INDEX ux_app_portfolio_transaction_reversal ON app.portfolio_transaction USING btree (reverses_transaction_id) WHERE (reverses_transaction_id IS NOT NULL);

CREATE INDEX ix_ingest_run_latest ON ingest.run USING btree (source_id, finished_at DESC);

CREATE INDEX ix_ingest_source_operational_health ON ingest.source USING btree (operational_state, enabled, health_owner);

CREATE INDEX ix_source_lifecycle_history_pit ON ingest.source_lifecycle_history USING btree (source_id, effective_at DESC, id DESC);

CREATE INDEX ix_ops_job_run_due ON ops.job_run USING btree (job_name, scheduled_due_at DESC);

CREATE INDEX ix_provider_lease_active ON ops.provider_lease USING btree (provider, expires_at DESC, workload, symbol);

CREATE INDEX ix_storage_archive_manifest_status ON ops.storage_archive_manifest USING btree (verification_status, archive_kind, created_at DESC);

CREATE UNIQUE INDEX uq_ops_job_run_running ON ops.job_run USING btree (job_name) WHERE (status = 'running'::text);

CREATE INDEX ix_market_observation_pit ON raw.market_observation USING btree (field_name, observed_at, available_at);

CREATE INDEX ix_market_observation_source ON raw.market_observation USING btree (source_id, available_at DESC);

CREATE INDEX ix_raw_content_item_observed ON raw.content_item USING btree (observed_at DESC);

CREATE INDEX ix_raw_fundamental_observation_payload ON raw.fundamental_observation USING btree (payload_id) WHERE (payload_id IS NOT NULL);

CREATE INDEX ix_raw_market_event_point_in_time ON raw.market_event USING btree (event_kind, starts_at, available_at);

CREATE INDEX ix_raw_market_event_version_point_in_time ON raw.market_event_version USING btree (event_kind, starts_at, available_at);

CREATE INDEX ix_raw_option_capture_generation_snapshot_state ON raw.option_capture_generation USING btree (snapshot_id, capture_state, generation DESC);

CREATE INDEX ix_raw_option_quote_contract ON ONLY raw.option_quote USING btree (contract_id, observed_at DESC) INCLUDE (mid, provider_iv, underlying_price);

CREATE INDEX ix_raw_option_quote_generation_group ON ONLY raw.option_quote USING btree (capture_generation_id, capture_group_key, contract_id);

CREATE INDEX ix_raw_option_quote_history_chain ON ONLY raw.option_quote USING btree (snapshot_id, contract_id) INCLUDE (provider_iv, bid, ask, volume, open_interest);

CREATE INDEX ix_raw_option_snapshot_history_lookup ON raw.option_snapshot USING btree (history_symbol, collection_profile, capture_state, slot_at DESC);

CREATE INDEX ix_raw_price_bar_history_asof ON raw.price_bar_history USING btree (instrument_id, trading_date, available_at DESC);

CREATE INDEX ix_raw_price_bar_history_panel_latest ON raw.price_bar_history USING btree (instrument_id, "interval", trading_date DESC, observed_at DESC) INCLUDE (close, available_at, source_id);

CREATE INDEX ix_raw_price_bar_lookup ON raw.price_bar USING btree (instrument_id, trading_date DESC) INCLUDE (close, volume);

CREATE INDEX ix_raw_quote_history_asof ON raw.quote_history USING btree (instrument_id, observed_at, available_at DESC);

CREATE INDEX ix_raw_quote_history_panel_latest ON raw.quote_history USING btree (instrument_id, observed_at DESC, available_at DESC) INCLUDE (price, change_pct, change_abs, source_id);

CREATE INDEX ix_raw_quote_lookup ON raw.quote USING btree (instrument_id, observed_at DESC) INCLUDE (price, source_id);

CREATE INDEX ix_raw_quote_panel_latest ON raw.quote USING btree (instrument_id, observed_at DESC, available_at DESC) INCLUDE (price, change_pct, change_abs, source_id);

CREATE INDEX option_quote_default_capture_generation_id_capture_group_ke_idx ON raw.option_quote_default USING btree (capture_generation_id, capture_group_key, contract_id);

CREATE UNIQUE INDEX ux_raw_option_quote_generation_contract_observed ON ONLY raw.option_quote USING btree (capture_generation_id, contract_id, observed_at) WHERE (capture_generation_id IS NOT NULL);

CREATE UNIQUE INDEX option_quote_default_capture_generation_id_contract_id_obse_idx ON raw.option_quote_default USING btree (capture_generation_id, contract_id, observed_at) WHERE (capture_generation_id IS NOT NULL);

CREATE INDEX option_quote_default_contract_id_observed_at_mid_provider_i_idx ON raw.option_quote_default USING btree (contract_id, observed_at DESC) INCLUDE (mid, provider_iv, underlying_price);

CREATE INDEX option_quote_default_snapshot_id_contract_id_provider_iv_bi_idx ON raw.option_quote_default USING btree (snapshot_id, contract_id) INCLUDE (provider_iv, bid, ask, volume, open_interest);

CREATE UNIQUE INDEX ux_raw_fundamental_observation_period ON raw.fundamental_observation USING btree (instrument_id, source_id, metric_set, period_end, observed_at, COALESCE(period_start, '0001-01-01'::date));

CREATE UNIQUE INDEX ux_raw_option_snapshot_history_slot ON raw.option_snapshot USING btree (source_id, collection_profile, history_symbol, slot_at) WHERE (collection_profile = 'history_full'::text);

ALTER INDEX raw.ix_raw_option_quote_generation_group ATTACH PARTITION raw.option_quote_default_capture_generation_id_capture_group_ke_idx;

ALTER INDEX raw.ux_raw_option_quote_generation_contract_observed ATTACH PARTITION raw.option_quote_default_capture_generation_id_contract_id_obse_idx;

ALTER INDEX raw.ix_raw_option_quote_contract ATTACH PARTITION raw.option_quote_default_contract_id_observed_at_mid_provider_i_idx;

ALTER INDEX raw.option_quote_snapshot_id_contract_id_observed_at_key ATTACH PARTITION raw.option_quote_default_snapshot_id_contract_id_observed_at_key;

ALTER INDEX raw.ix_raw_option_quote_history_chain ATTACH PARTITION raw.option_quote_default_snapshot_id_contract_id_provider_iv_bi_idx;

CREATE TRIGGER agent_task_payload_availability BEFORE INSERT OR UPDATE ON analysis.agent_task FOR EACH ROW EXECUTE FUNCTION analysis.stamp_agent_task_payload_availability();

CREATE TRIGGER assign_research_gate_actual_clock BEFORE INSERT ON analysis.validation_gate_result FOR EACH ROW EXECUTE FUNCTION analysis.assign_research_gate_actual_clock();

CREATE TRIGGER book_attribution_immutable BEFORE DELETE OR UPDATE ON analysis.book_attribution FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

CREATE TRIGGER enforce_experiment_manifest_immutable BEFORE DELETE OR UPDATE ON analysis.experiment_manifest FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_manifest_immutable();

CREATE TRIGGER enforce_phase3_strategy_status BEFORE INSERT OR UPDATE OF status, promotability, actionability, strategy_family, strategy_key, mechanism_class, p3_enabled ON analysis.strategy_revision FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_strategy_status();

CREATE TRIGGER enforce_research_authority_availability BEFORE INSERT ON analysis.experiment_family FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_authority_availability();

CREATE TRIGGER enforce_research_authority_availability BEFORE INSERT ON analysis.experiment_manifest FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_authority_availability();

CREATE TRIGGER enforce_research_authority_availability BEFORE INSERT ON analysis.hypothesis FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_authority_availability();

CREATE TRIGGER enforce_research_authority_availability BEFORE INSERT ON analysis.trial_universe_manifest FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_authority_availability();

CREATE TRIGGER enforce_research_evaluator_output BEFORE INSERT OR DELETE OR UPDATE ON analysis.research_evaluator_output FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_evaluator_output();

CREATE TRIGGER enforce_research_evidence_manifest BEFORE INSERT OR DELETE OR UPDATE ON analysis.research_evidence_manifest FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_evidence_manifest();

CREATE TRIGGER enforce_research_gate_actual_availability BEFORE INSERT ON analysis.validation_gate_result FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_gate_actual_availability();

CREATE TRIGGER enforce_research_gate_pit BEFORE INSERT ON analysis.validation_gate_result FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_gate_pit();

CREATE TRIGGER enforce_research_gate_promotion_clock BEFORE UPDATE ON analysis.strategy_revision FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_gate_promotion_clock();

CREATE TRIGGER enforce_research_result_actual_availability BEFORE INSERT ON analysis.trial_result FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_result_actual_availability();

CREATE TRIGGER enforce_research_result_pit BEFORE INSERT ON analysis.trial_result FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_result_pit();

CREATE TRIGGER enforce_research_revision_promotion BEFORE INSERT OR UPDATE OF status, research_required, hypothesis_id, experiment_family_id, artifact_id, artifact_hash ON analysis.strategy_revision FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_revision_promotion();

CREATE CONSTRAINT TRIGGER enforce_research_revision_promotion_hardened AFTER INSERT OR UPDATE ON analysis.strategy_revision DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_revision_promotion_hardened();

CREATE TRIGGER enforce_research_trial_terminal_immutability BEFORE INSERT OR DELETE OR UPDATE ON analysis.research_trial FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_trial_terminal_immutability();

CREATE TRIGGER enforce_research_universe_actual_availability BEFORE INSERT ON analysis.universe_observation FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_universe_actual_availability();

CREATE TRIGGER enforce_research_universe_pit BEFORE INSERT ON analysis.universe_observation FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_universe_pit();

CREATE TRIGGER enforce_strategy_comparison_immutable BEFORE DELETE OR UPDATE ON analysis.strategy_comparison FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_immutable();

CREATE TRIGGER enforce_strategy_comparison_lineage BEFORE INSERT ON analysis.strategy_comparison FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_comparison();

CREATE TRIGGER enforce_strategy_evaluation_availability BEFORE INSERT OR DELETE OR UPDATE ON analysis.strategy_evaluation FOR EACH ROW EXECUTE FUNCTION analysis.enforce_strategy_evaluation_availability();

CREATE TRIGGER enforce_strategy_forecast_authority BEFORE INSERT OR UPDATE ON analysis.strategy_forecast FOR EACH ROW EXECUTE FUNCTION analysis.enforce_strategy_forecast_authority();

CREATE TRIGGER enforce_strategy_forecast_immutable BEFORE DELETE OR UPDATE ON analysis.strategy_forecast FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_rows_immutable();

CREATE TRIGGER enforce_strategy_forecast_phase3_link BEFORE INSERT OR UPDATE ON analysis.strategy_forecast FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_forecast_link();

CREATE TRIGGER enforce_strategy_manifest_hash BEFORE INSERT ON analysis.strategy_manifest FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_manifest_hash();

CREATE TRIGGER enforce_strategy_manifest_immutable BEFORE DELETE OR UPDATE ON analysis.strategy_manifest FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_immutable();

CREATE TRIGGER enforce_strategy_monitoring_immutable BEFORE DELETE OR UPDATE ON analysis.strategy_monitoring_evidence FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_immutable();

CREATE TRIGGER enforce_strategy_monitoring_lineage BEFORE INSERT ON analysis.strategy_monitoring_evidence FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_monitoring();

CREATE TRIGGER enforce_strategy_pnl_tape_immutable BEFORE DELETE OR UPDATE ON analysis.strategy_pnl_tape FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_immutable();

CREATE TRIGGER enforce_strategy_pnl_tape_lineage BEFORE INSERT ON analysis.strategy_pnl_tape FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_pnl_tape();

CREATE TRIGGER enforce_strategy_revision_parameters_immutable BEFORE UPDATE OF parameters ON analysis.strategy_revision FOR EACH ROW EXECUTE FUNCTION analysis.enforce_strategy_revision_parameters_immutable();

CREATE TRIGGER enforce_trial_result_immutable BEFORE DELETE OR UPDATE ON analysis.trial_result FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_rows_immutable();

CREATE TRIGGER enforce_trial_universe_manifest_immutable BEFORE DELETE OR UPDATE ON analysis.trial_universe_manifest FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_manifest_immutable();

CREATE TRIGGER enforce_universe_observation_immutable BEFORE DELETE OR UPDATE ON analysis.universe_observation FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_rows_immutable();

CREATE TRIGGER enforce_validation_dossier_seal BEFORE INSERT OR DELETE OR UPDATE ON analysis.validation_dossier FOR EACH ROW EXECUTE FUNCTION analysis.enforce_validation_dossier_seal();

CREATE TRIGGER enforce_validation_gate_result_immutable BEFORE DELETE OR UPDATE ON analysis.validation_gate_result FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_rows_immutable();

CREATE TRIGGER execution_model_snapshot_immutable BEFORE DELETE OR UPDATE ON analysis.execution_model_snapshot FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

CREATE TRIGGER market_coverage_vector_immutable BEFORE DELETE OR UPDATE ON analysis.market_coverage_vector FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase2_update();

CREATE TRIGGER market_scenario_path_immutable BEFORE DELETE OR UPDATE ON analysis.market_scenario_path FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase2_update();

CREATE TRIGGER market_state_posterior_immutable BEFORE DELETE OR UPDATE ON analysis.market_state_posterior FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase2_update();

CREATE TRIGGER option_liquidity_sla_immutable BEFORE DELETE OR UPDATE ON analysis.option_liquidity_sla FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase2_update();

CREATE TRIGGER phase4_attribution_multiplier_guard BEFORE INSERT ON analysis.book_attribution FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_attribution_multiplier_guard();

CREATE CONSTRAINT TRIGGER phase4_authority_lineage AFTER INSERT ON analysis.portfolio_allocation_item DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_authority_lineage();

CREATE CONSTRAINT TRIGGER phase4_authority_snapshot_complete AFTER INSERT ON analysis.portfolio_allocation_snapshot DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_authority_snapshot_complete();

CREATE TRIGGER phase4_execution_snapshot_guard BEFORE INSERT ON analysis.execution_model_snapshot FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_execution_snapshot_guard();

CREATE CONSTRAINT TRIGGER phase4_funding_conservation AFTER INSERT ON analysis.portfolio_allocation_item DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_funding_conservation();

CREATE TRIGGER phase4_review_item_guard BEFORE INSERT ON analysis.portfolio_allocation_item FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_review_item_guard();

CREATE TRIGGER phase4_review_snapshot_guard BEFORE INSERT ON analysis.portfolio_allocation_snapshot FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_review_snapshot_guard();

CREATE CONSTRAINT TRIGGER phase4_source_lineage AFTER INSERT ON analysis.portfolio_allocation_item DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_source_lineage();

CREATE TRIGGER portfolio_allocation_item_immutable BEFORE DELETE OR UPDATE ON analysis.portfolio_allocation_item FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

CREATE TRIGGER portfolio_allocation_item_lineage BEFORE INSERT ON analysis.portfolio_allocation_item FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_allocation_item_funding_lineage();

CREATE TRIGGER portfolio_allocation_snapshot_immutable BEFORE DELETE OR UPDATE ON analysis.portfolio_allocation_snapshot FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

CREATE TRIGGER portfolio_allocation_snapshot_lineage BEFORE INSERT ON analysis.portfolio_allocation_snapshot FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();

CREATE TRIGGER portfolio_drift_evidence_immutable BEFORE DELETE OR UPDATE ON analysis.portfolio_drift_evidence FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

CREATE TRIGGER portfolio_drift_lineage BEFORE INSERT ON analysis.portfolio_drift_evidence FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();

CREATE TRIGGER portfolio_scenario_artifact_immutable BEFORE DELETE OR UPDATE ON analysis.probabilistic_portfolio_scenario_artifact FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

CREATE TRIGGER portfolio_scenario_lineage BEFORE INSERT ON analysis.probabilistic_portfolio_scenario_artifact FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();

CREATE TRIGGER tr_option_event_assign_current_cohort BEFORE INSERT ON analysis.option_event FOR EACH ROW EXECUTE FUNCTION analysis.assign_current_option_recovery_cohort();

CREATE TRIGGER zzz_phase4_funding_content BEFORE INSERT ON analysis.portfolio_allocation_item FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_funding_content();

CREATE TRIGGER capture_portfolio_transaction_sector BEFORE INSERT ON app.portfolio_transaction FOR EACH ROW EXECUTE FUNCTION app.capture_portfolio_transaction_sector();

CREATE TRIGGER manual_account_snapshot_immutable BEFORE DELETE OR UPDATE ON app.manual_account_snapshot FOR EACH ROW EXECUTE FUNCTION app.prevent_manual_account_snapshot_mutation();

CREATE TRIGGER paper_execution_lineage BEFORE INSERT ON app.paper_execution_observation FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_paper_execution();

CREATE TRIGGER paper_execution_observation_immutable BEFORE DELETE OR UPDATE ON app.paper_execution_observation FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

CREATE TRIGGER paper_order_leg_append_only BEFORE INSERT OR DELETE OR UPDATE ON app.paper_order_leg FOR EACH ROW EXECUTE FUNCTION app.prevent_paper_order_leg_mutation();

CREATE CONSTRAINT TRIGGER paper_order_leg_set_complete AFTER INSERT OR UPDATE ON app.paper_order DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.require_complete_paper_order_leg_set();

CREATE TRIGGER paper_order_ticket_immutable BEFORE DELETE OR UPDATE ON app.paper_order FOR EACH ROW EXECUTE FUNCTION app.prevent_paper_order_ticket_mutation();

CREATE TRIGGER phase4_paper_execution_guard BEFORE INSERT ON app.paper_execution_observation FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_paper_execution_guard();

CREATE TRIGGER payload_identity_immutable BEFORE UPDATE ON ingest.payload FOR EACH ROW EXECUTE FUNCTION ingest.reject_identity_update();

CREATE TRIGGER run_identity_immutable BEFORE UPDATE ON ingest.run FOR EACH ROW EXECUTE FUNCTION ingest.reject_identity_update();

CREATE TRIGGER source_lifecycle_change AFTER UPDATE OF enabled, operational_state ON ingest.source FOR EACH ROW WHEN (((old.enabled IS DISTINCT FROM new.enabled) OR (old.operational_state IS DISTINCT FROM new.operational_state))) EXECUTE FUNCTION ingest.record_source_lifecycle();

CREATE TRIGGER source_lifecycle_insert AFTER INSERT ON ingest.source FOR EACH ROW EXECUTE FUNCTION ingest.record_source_lifecycle();

CREATE TRIGGER market_observation_immutable BEFORE DELETE OR UPDATE ON raw.market_observation FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase2_update();

CREATE TRIGGER price_bar_confirmation_projection AFTER INSERT OR UPDATE OF fact_id, fact_available_at, ingest_run_id ON raw.price_bar_confirmation FOR EACH ROW EXECUTE FUNCTION raw.project_confirmation_staging();

CREATE TRIGGER quote_confirmation_projection AFTER INSERT OR UPDATE OF fact_id, fact_available_at, ingest_run_id ON raw.quote_confirmation FOR EACH ROW EXECUTE FUNCTION raw.project_confirmation_staging();

ALTER TABLE ONLY analysis.agent_run
    ADD CONSTRAINT agent_run_experiment_id_fkey FOREIGN KEY (experiment_id) REFERENCES analysis.agent_experiment(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.agent_task
    ADD CONSTRAINT agent_task_agent_run_id_fkey FOREIGN KEY (agent_run_id) REFERENCES analysis.agent_run(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.agent_task
    ADD CONSTRAINT agent_task_decision_id_fkey FOREIGN KEY (decision_id) REFERENCES analysis.decision(id);

ALTER TABLE ONLY analysis.agent_task
    ADD CONSTRAINT agent_task_experiment_id_fkey FOREIGN KEY (experiment_id) REFERENCES analysis.agent_experiment(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.agent_task
    ADD CONSTRAINT agent_task_paired_task_id_fkey FOREIGN KEY (paired_task_id) REFERENCES analysis.agent_task(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.book_attribution
    ADD CONSTRAINT book_attribution_allocation_id_fkey FOREIGN KEY (allocation_id) REFERENCES analysis.portfolio_allocation_snapshot(allocation_id);

ALTER TABLE ONLY analysis.book_attribution
    ADD CONSTRAINT book_attribution_allocation_item_id_fkey FOREIGN KEY (allocation_item_id) REFERENCES analysis.portfolio_allocation_item(allocation_item_id);

ALTER TABLE ONLY analysis.book_attribution
    ADD CONSTRAINT book_attribution_hypothesis_id_fkey FOREIGN KEY (hypothesis_id) REFERENCES analysis.hypothesis(id);

ALTER TABLE ONLY analysis.book_attribution
    ADD CONSTRAINT book_attribution_paper_execution_observation_id_fkey FOREIGN KEY (paper_execution_observation_id) REFERENCES app.paper_execution_observation(paper_execution_observation_id);

ALTER TABLE ONLY analysis.book_attribution
    ADD CONSTRAINT book_attribution_result_id_fkey FOREIGN KEY (result_id) REFERENCES analysis.trial_result(id);

ALTER TABLE ONLY analysis.book_attribution
    ADD CONSTRAINT book_attribution_strategy_forecast_id_fkey FOREIGN KEY (strategy_forecast_id) REFERENCES analysis.strategy_forecast(id);

ALTER TABLE ONLY analysis.book_attribution
    ADD CONSTRAINT book_attribution_trial_id_fkey FOREIGN KEY (trial_id) REFERENCES analysis.research_trial(id);

ALTER TABLE ONLY analysis.decision_evidence
    ADD CONSTRAINT decision_evidence_decision_id_fkey FOREIGN KEY (decision_id) REFERENCES analysis.decision(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.decision
    ADD CONSTRAINT decision_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY analysis.decision
    ADD CONSTRAINT decision_run_id_fkey FOREIGN KEY (run_id) REFERENCES analysis.run(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.decision
    ADD CONSTRAINT decision_strategy_revision_id_fkey FOREIGN KEY (strategy_revision_id) REFERENCES analysis.strategy_revision(id);

ALTER TABLE ONLY analysis.event_scout_event
    ADD CONSTRAINT event_scout_event_event_id_fkey FOREIGN KEY (event_id) REFERENCES analysis.event_decision_packet(event_id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.event_study_feature
    ADD CONSTRAINT event_study_feature_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.event_study_feature
    ADD CONSTRAINT event_study_feature_market_event_id_fkey FOREIGN KEY (market_event_id) REFERENCES raw.market_event(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.event_study_feature
    ADD CONSTRAINT event_study_feature_market_event_version_id_fkey FOREIGN KEY (market_event_version_id) REFERENCES raw.market_event_version(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.event_study_feature
    ADD CONSTRAINT event_study_feature_run_id_fkey FOREIGN KEY (run_id) REFERENCES analysis.run(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.execution_model_snapshot
    ADD CONSTRAINT execution_model_snapshot_allocation_id_fkey FOREIGN KEY (allocation_id) REFERENCES analysis.portfolio_allocation_snapshot(allocation_id);

ALTER TABLE ONLY analysis.experiment_family
    ADD CONSTRAINT experiment_family_hypothesis_id_fkey FOREIGN KEY (hypothesis_id) REFERENCES analysis.hypothesis(id);

ALTER TABLE ONLY analysis.experiment_manifest
    ADD CONSTRAINT experiment_manifest_experiment_family_id_fkey FOREIGN KEY (experiment_family_id) REFERENCES analysis.experiment_family(id);

ALTER TABLE ONLY analysis.option_outcome
    ADD CONSTRAINT fk_option_outcome_shadow_trade FOREIGN KEY (shadow_trade_id) REFERENCES analysis.shadow_trade(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_decision
    ADD CONSTRAINT option_decision_contract_id_fkey FOREIGN KEY (contract_id) REFERENCES catalog.option_contract(id);

ALTER TABLE ONLY analysis.option_decision
    ADD CONSTRAINT option_decision_decision_id_fkey FOREIGN KEY (decision_id) REFERENCES analysis.decision(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.option_decision
    ADD CONSTRAINT option_decision_primary_decision_id_fkey FOREIGN KEY (primary_decision_id) REFERENCES analysis.decision(id);

ALTER TABLE ONLY analysis.option_decision
    ADD CONSTRAINT option_decision_relative_value_id_fkey FOREIGN KEY (relative_value_id) REFERENCES analysis.option_relative_value(id);

ALTER TABLE ONLY analysis.option_decision
    ADD CONSTRAINT option_decision_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES raw.option_snapshot(id);

ALTER TABLE ONLY analysis.option_decision
    ADD CONSTRAINT option_decision_thesis_id_fkey FOREIGN KEY (thesis_id) REFERENCES app.thesis(id);

ALTER TABLE ONLY analysis.option_discovery_candidate
    ADD CONSTRAINT option_discovery_candidate_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY analysis.option_discovery_candidate
    ADD CONSTRAINT option_discovery_candidate_run_id_fkey FOREIGN KEY (run_id) REFERENCES analysis.option_discovery_run(run_id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.option_discovery_run
    ADD CONSTRAINT option_discovery_run_run_id_fkey FOREIGN KEY (run_id) REFERENCES analysis.run(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.option_event_agent_batch
    ADD CONSTRAINT option_event_agent_batch_agent_run_id_fkey FOREIGN KEY (agent_run_id) REFERENCES analysis.agent_run(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_event_agent_batch
    ADD CONSTRAINT option_event_agent_batch_capture_id_fkey FOREIGN KEY (capture_id) REFERENCES analysis.option_event_capture(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_event_agent_batch
    ADD CONSTRAINT option_event_agent_batch_cohort_id_fkey FOREIGN KEY (cohort_id) REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_event_agent_batch
    ADD CONSTRAINT option_event_agent_batch_event_id_fkey FOREIGN KEY (event_id) REFERENCES analysis.option_event(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.option_event_agent_batch
    ADD CONSTRAINT option_event_agent_batch_experiment_id_fkey FOREIGN KEY (experiment_id) REFERENCES analysis.agent_experiment(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_event_agent_batch
    ADD CONSTRAINT option_event_agent_batch_paired_task_id_fkey FOREIGN KEY (paired_task_id) REFERENCES analysis.agent_task(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_event_capture
    ADD CONSTRAINT option_event_capture_capture_generation_id_fkey FOREIGN KEY (capture_generation_id) REFERENCES raw.option_capture_generation(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_event_capture
    ADD CONSTRAINT option_event_capture_event_id_fkey FOREIGN KEY (event_id) REFERENCES analysis.option_event(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.option_event_capture
    ADD CONSTRAINT option_event_capture_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES raw.option_snapshot(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_event
    ADD CONSTRAINT option_event_cohort_id_fkey FOREIGN KEY (cohort_id) REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_event_contract
    ADD CONSTRAINT option_event_contract_contract_id_fkey FOREIGN KEY (contract_id) REFERENCES catalog.option_contract(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_event_contract
    ADD CONSTRAINT option_event_contract_event_id_fkey FOREIGN KEY (event_id) REFERENCES analysis.option_event(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.option_event_contract
    ADD CONSTRAINT option_event_contract_initial_capture_generation_id_fkey FOREIGN KEY (initial_capture_generation_id) REFERENCES raw.option_capture_generation(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_event_contract
    ADD CONSTRAINT option_event_contract_replaces_contract_id_fkey FOREIGN KEY (replaces_contract_id) REFERENCES analysis.option_event_contract(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_event_detector_run
    ADD CONSTRAINT option_event_detector_run_cohort_id_fkey FOREIGN KEY (cohort_id) REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_event_detector_run
    ADD CONSTRAINT option_event_detector_run_provider_run_id_fkey FOREIGN KEY (provider_run_id) REFERENCES ingest.run(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_event
    ADD CONSTRAINT option_event_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_event_signal
    ADD CONSTRAINT option_event_signal_capture_id_fkey FOREIGN KEY (capture_id) REFERENCES analysis.option_event_capture(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_event_signal
    ADD CONSTRAINT option_event_signal_cohort_id_fkey FOREIGN KEY (cohort_id) REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_event_signal
    ADD CONSTRAINT option_event_signal_contract_id_fkey FOREIGN KEY (contract_id) REFERENCES catalog.option_contract(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_event_signal
    ADD CONSTRAINT option_event_signal_decision_id_fkey FOREIGN KEY (decision_id) REFERENCES analysis.decision(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_event_signal
    ADD CONSTRAINT option_event_signal_event_contract_id_fkey FOREIGN KEY (event_contract_id) REFERENCES analysis.option_event_contract(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_event_signal
    ADD CONSTRAINT option_event_signal_event_id_fkey FOREIGN KEY (event_id) REFERENCES analysis.option_event(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.option_event_signal
    ADD CONSTRAINT option_event_signal_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES raw.option_snapshot(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_event_signal
    ADD CONSTRAINT option_event_signal_strategy_revision_id_fkey FOREIGN KEY (strategy_revision_id) REFERENCES analysis.strategy_revision(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_event_spot
    ADD CONSTRAINT option_event_spot_event_id_fkey FOREIGN KEY (event_id) REFERENCES analysis.option_event(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.option_feature
    ADD CONSTRAINT option_feature_contract_id_fkey FOREIGN KEY (contract_id) REFERENCES catalog.option_contract(id);

ALTER TABLE ONLY analysis.option_feature
    ADD CONSTRAINT option_feature_run_id_fkey FOREIGN KEY (run_id) REFERENCES analysis.run(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.option_feature
    ADD CONSTRAINT option_feature_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES raw.option_snapshot(id);

ALTER TABLE ONLY analysis.option_gate_result
    ADD CONSTRAINT option_gate_result_run_id_instrument_id_fkey FOREIGN KEY (run_id, instrument_id) REFERENCES analysis.option_discovery_candidate(run_id, instrument_id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.option_history_anomaly
    ADD CONSTRAINT option_history_anomaly_contract_id_fkey FOREIGN KEY (contract_id) REFERENCES catalog.option_contract(id);

ALTER TABLE ONLY analysis.option_history_anomaly
    ADD CONSTRAINT option_history_anomaly_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES raw.option_snapshot(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.option_liquidity_sla
    ADD CONSTRAINT option_liquidity_sla_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY analysis.option_liquidity_sla
    ADD CONSTRAINT option_liquidity_sla_payload_id_fkey FOREIGN KEY (payload_id) REFERENCES ingest.payload(id);

ALTER TABLE ONLY analysis.option_liquidity_sla
    ADD CONSTRAINT option_liquidity_sla_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id);

ALTER TABLE ONLY analysis.option_opportunity_observation
    ADD CONSTRAINT option_opportunity_observation_capture_generation_id_fkey FOREIGN KEY (capture_generation_id) REFERENCES raw.option_capture_generation(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_opportunity_observation
    ADD CONSTRAINT option_opportunity_observation_capture_id_fkey FOREIGN KEY (capture_id) REFERENCES analysis.option_event_capture(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_opportunity_observation
    ADD CONSTRAINT option_opportunity_observation_cohort_id_fkey FOREIGN KEY (cohort_id) REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_opportunity_observation
    ADD CONSTRAINT option_opportunity_observation_contract_id_fkey FOREIGN KEY (contract_id) REFERENCES catalog.option_contract(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_opportunity_observation
    ADD CONSTRAINT option_opportunity_observation_event_contract_id_fkey FOREIGN KEY (event_contract_id) REFERENCES analysis.option_event_contract(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_opportunity_observation
    ADD CONSTRAINT option_opportunity_observation_event_id_fkey FOREIGN KEY (event_id) REFERENCES analysis.option_event(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.option_opportunity_observation
    ADD CONSTRAINT option_opportunity_observation_paper_order_id_fkey FOREIGN KEY (paper_order_id) REFERENCES app.paper_order(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_opportunity_observation
    ADD CONSTRAINT option_opportunity_observation_signal_id_fkey FOREIGN KEY (signal_id) REFERENCES analysis.option_event_signal(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_opportunity_observation
    ADD CONSTRAINT option_opportunity_observation_strategy_revision_id_fkey FOREIGN KEY (strategy_revision_id) REFERENCES analysis.strategy_revision(id) ON DELETE SET NULL;

ALTER TABLE ONLY analysis.option_outcome
    ADD CONSTRAINT option_outcome_decision_id_fkey FOREIGN KEY (decision_id) REFERENCES analysis.decision(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.option_recovery_event_session_quality
    ADD CONSTRAINT option_recovery_event_session_quality_cohort_id_fkey FOREIGN KEY (cohort_id) REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_recovery_event_session_quality
    ADD CONSTRAINT option_recovery_event_session_quality_event_id_fkey FOREIGN KEY (event_id) REFERENCES analysis.option_event(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.option_recovery_program_session
    ADD CONSTRAINT option_recovery_program_session_cohort_id_fkey FOREIGN KEY (cohort_id) REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_relative_value
    ADD CONSTRAINT option_relative_value_analysis_run_id_fkey FOREIGN KEY (analysis_run_id) REFERENCES analysis.run(id) ON DELETE CASCADE NOT VALID;

ALTER TABLE ONLY analysis.option_relative_value
    ADD CONSTRAINT option_relative_value_capture_generation_id_fkey FOREIGN KEY (capture_generation_id) REFERENCES raw.option_capture_generation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_relative_value
    ADD CONSTRAINT option_relative_value_contract_id_fkey FOREIGN KEY (contract_id) REFERENCES catalog.option_contract(id);

ALTER TABLE ONLY analysis.option_relative_value_verification
    ADD CONSTRAINT option_relative_value_verification_relative_value_id_fkey FOREIGN KEY (relative_value_id) REFERENCES analysis.option_relative_value(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_surface_shift
    ADD CONSTRAINT option_surface_shift_current_analysis_run_id_fkey FOREIGN KEY (current_analysis_run_id) REFERENCES analysis.run(id) ON DELETE CASCADE NOT VALID;

ALTER TABLE ONLY analysis.option_surface_shift
    ADD CONSTRAINT option_surface_shift_current_capture_generation_id_fkey FOREIGN KEY (current_capture_generation_id) REFERENCES raw.option_capture_generation(id);

ALTER TABLE ONLY analysis.option_surface_shift
    ADD CONSTRAINT option_surface_shift_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.option_surface_shift
    ADD CONSTRAINT option_surface_shift_previous_analysis_run_id_fkey FOREIGN KEY (previous_analysis_run_id) REFERENCES analysis.run(id) ON DELETE CASCADE NOT VALID;

ALTER TABLE ONLY analysis.option_surface_shift
    ADD CONSTRAINT option_surface_shift_previous_capture_generation_id_fkey FOREIGN KEY (previous_capture_generation_id) REFERENCES raw.option_capture_generation(id);

ALTER TABLE ONLY analysis.option_surface_summary
    ADD CONSTRAINT option_surface_summary_analysis_run_id_fkey FOREIGN KEY (analysis_run_id) REFERENCES analysis.run(id) ON DELETE CASCADE NOT VALID;

ALTER TABLE ONLY analysis.option_surface_summary
    ADD CONSTRAINT option_surface_summary_capture_generation_id_fkey FOREIGN KEY (capture_generation_id) REFERENCES raw.option_capture_generation(id);

ALTER TABLE ONLY analysis.option_surface_summary
    ADD CONSTRAINT option_surface_summary_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES raw.option_snapshot(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.portfolio_allocation_item
    ADD CONSTRAINT portfolio_allocation_item_allocation_id_fkey FOREIGN KEY (allocation_id) REFERENCES analysis.portfolio_allocation_snapshot(allocation_id);

ALTER TABLE ONLY analysis.portfolio_allocation_item
    ADD CONSTRAINT portfolio_allocation_item_hypothesis_id_fkey FOREIGN KEY (hypothesis_id) REFERENCES analysis.hypothesis(id);

ALTER TABLE ONLY analysis.portfolio_allocation_item
    ADD CONSTRAINT portfolio_allocation_item_strategy_forecast_id_fkey FOREIGN KEY (strategy_forecast_id) REFERENCES analysis.strategy_forecast(id);

ALTER TABLE ONLY analysis.portfolio_drift_evidence
    ADD CONSTRAINT portfolio_drift_evidence_allocation_id_fkey FOREIGN KEY (allocation_id) REFERENCES analysis.portfolio_allocation_snapshot(allocation_id);

ALTER TABLE ONLY analysis.portfolio_drift_evidence
    ADD CONSTRAINT portfolio_drift_evidence_allocation_item_id_fkey FOREIGN KEY (allocation_item_id) REFERENCES analysis.portfolio_allocation_item(allocation_item_id);

ALTER TABLE ONLY analysis.probabilistic_portfolio_scenario_artifact
    ADD CONSTRAINT probabilistic_portfolio_scenario_artifact_allocation_id_fkey FOREIGN KEY (allocation_id) REFERENCES analysis.portfolio_allocation_snapshot(allocation_id);

ALTER TABLE ONLY analysis.reject_summary
    ADD CONSTRAINT reject_summary_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY analysis.reject_summary
    ADD CONSTRAINT reject_summary_run_id_fkey FOREIGN KEY (run_id) REFERENCES analysis.run(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.reject_summary
    ADD CONSTRAINT reject_summary_strategy_revision_id_fkey FOREIGN KEY (strategy_revision_id) REFERENCES analysis.strategy_revision(id);

ALTER TABLE ONLY analysis.research_evaluator_output
    ADD CONSTRAINT research_evaluator_output_analysis_run_id_fkey FOREIGN KEY (analysis_run_id) REFERENCES analysis.run(id);

ALTER TABLE ONLY analysis.research_evaluator_output
    ADD CONSTRAINT research_evaluator_output_research_trial_id_fkey FOREIGN KEY (research_trial_id) REFERENCES analysis.research_trial(id);

ALTER TABLE ONLY analysis.research_evaluator_output
    ADD CONSTRAINT research_evaluator_output_trial_result_id_fkey FOREIGN KEY (trial_result_id) REFERENCES analysis.trial_result(id);

ALTER TABLE ONLY analysis.research_evidence_manifest
    ADD CONSTRAINT research_evidence_manifest_evaluator_output_id_fkey FOREIGN KEY (evaluator_output_id) REFERENCES analysis.research_evaluator_output(id);

ALTER TABLE ONLY analysis.research_evidence_manifest
    ADD CONSTRAINT research_evidence_manifest_research_trial_id_fkey FOREIGN KEY (research_trial_id) REFERENCES analysis.research_trial(id);

ALTER TABLE ONLY analysis.research_evidence_manifest
    ADD CONSTRAINT research_evidence_manifest_trial_result_id_fkey FOREIGN KEY (trial_result_id) REFERENCES analysis.trial_result(id);

ALTER TABLE ONLY analysis.research_trial
    ADD CONSTRAINT research_trial_experiment_family_id_fkey FOREIGN KEY (experiment_family_id) REFERENCES analysis.experiment_family(id);

ALTER TABLE ONLY analysis.run
    ADD CONSTRAINT run_strategy_revision_id_fkey FOREIGN KEY (strategy_revision_id) REFERENCES analysis.strategy_revision(id);

ALTER TABLE ONLY analysis.shadow_trade
    ADD CONSTRAINT shadow_trade_decision_id_fkey FOREIGN KEY (decision_id) REFERENCES analysis.decision(id);

ALTER TABLE ONLY analysis.shadow_trade
    ADD CONSTRAINT shadow_trade_entry_cohort_id_fkey FOREIGN KEY (entry_cohort_id) REFERENCES raw.option_capture_generation(id);

ALTER TABLE ONLY analysis.source_signal
    ADD CONSTRAINT source_signal_content_item_id_fkey FOREIGN KEY (content_item_id) REFERENCES raw.content_item(id);

ALTER TABLE ONLY analysis.source_signal
    ADD CONSTRAINT source_signal_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY analysis.source_signal
    ADD CONSTRAINT source_signal_run_id_fkey FOREIGN KEY (run_id) REFERENCES analysis.run(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.strategy_comparison
    ADD CONSTRAINT strategy_comparison_challenger_result_id_fkey FOREIGN KEY (challenger_result_id) REFERENCES analysis.trial_result(id);

ALTER TABLE ONLY analysis.strategy_comparison
    ADD CONSTRAINT strategy_comparison_challenger_revision_id_fkey FOREIGN KEY (challenger_revision_id) REFERENCES analysis.strategy_revision(id);

ALTER TABLE ONLY analysis.strategy_comparison
    ADD CONSTRAINT strategy_comparison_challenger_trial_id_fkey FOREIGN KEY (challenger_trial_id) REFERENCES analysis.research_trial(id);

ALTER TABLE ONLY analysis.strategy_comparison
    ADD CONSTRAINT strategy_comparison_champion_result_id_fkey FOREIGN KEY (champion_result_id) REFERENCES analysis.trial_result(id);

ALTER TABLE ONLY analysis.strategy_comparison
    ADD CONSTRAINT strategy_comparison_champion_revision_id_fkey FOREIGN KEY (champion_revision_id) REFERENCES analysis.strategy_revision(id);

ALTER TABLE ONLY analysis.strategy_comparison
    ADD CONSTRAINT strategy_comparison_champion_trial_id_fkey FOREIGN KEY (champion_trial_id) REFERENCES analysis.research_trial(id);

ALTER TABLE ONLY analysis.strategy_evaluation
    ADD CONSTRAINT strategy_evaluation_experiment_family_id_fkey FOREIGN KEY (experiment_family_id) REFERENCES analysis.experiment_family(id);

ALTER TABLE ONLY analysis.strategy_evaluation
    ADD CONSTRAINT strategy_evaluation_hypothesis_id_fkey FOREIGN KEY (hypothesis_id) REFERENCES analysis.hypothesis(id);

ALTER TABLE ONLY analysis.strategy_evaluation
    ADD CONSTRAINT strategy_evaluation_research_trial_id_fkey FOREIGN KEY (research_trial_id) REFERENCES analysis.research_trial(id);

ALTER TABLE ONLY analysis.strategy_evaluation
    ADD CONSTRAINT strategy_evaluation_strategy_revision_id_fkey FOREIGN KEY (strategy_revision_id) REFERENCES analysis.strategy_revision(id);

ALTER TABLE ONLY analysis.strategy_evaluation
    ADD CONSTRAINT strategy_evaluation_validation_dossier_id_fkey FOREIGN KEY (validation_dossier_id) REFERENCES analysis.validation_dossier(id);

ALTER TABLE ONLY analysis.strategy_forecast
    ADD CONSTRAINT strategy_forecast_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY analysis.strategy_forecast
    ADD CONSTRAINT strategy_forecast_research_trial_id_fkey FOREIGN KEY (research_trial_id) REFERENCES analysis.research_trial(id);

ALTER TABLE ONLY analysis.strategy_forecast
    ADD CONSTRAINT strategy_forecast_strategy_evaluation_id_fkey FOREIGN KEY (strategy_evaluation_id) REFERENCES analysis.strategy_evaluation(id);

ALTER TABLE ONLY analysis.strategy_forecast
    ADD CONSTRAINT strategy_forecast_strategy_revision_id_fkey FOREIGN KEY (strategy_revision_id) REFERENCES analysis.strategy_revision(id);

ALTER TABLE ONLY analysis.strategy_forecast
    ADD CONSTRAINT strategy_forecast_trial_result_id_fkey FOREIGN KEY (trial_result_id) REFERENCES analysis.trial_result(id);

ALTER TABLE ONLY analysis.strategy_manifest
    ADD CONSTRAINT strategy_manifest_strategy_revision_id_fkey FOREIGN KEY (strategy_revision_id) REFERENCES analysis.strategy_revision(id);

ALTER TABLE ONLY analysis.strategy_monitoring_evidence
    ADD CONSTRAINT strategy_monitoring_evidence_research_trial_id_fkey FOREIGN KEY (research_trial_id) REFERENCES analysis.research_trial(id);

ALTER TABLE ONLY analysis.strategy_monitoring_evidence
    ADD CONSTRAINT strategy_monitoring_evidence_strategy_revision_id_fkey FOREIGN KEY (strategy_revision_id) REFERENCES analysis.strategy_revision(id);

ALTER TABLE ONLY analysis.strategy_monitoring_evidence
    ADD CONSTRAINT strategy_monitoring_evidence_trial_result_id_fkey FOREIGN KEY (trial_result_id) REFERENCES analysis.trial_result(id);

ALTER TABLE ONLY analysis.strategy_pnl_tape
    ADD CONSTRAINT strategy_pnl_tape_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY analysis.strategy_pnl_tape
    ADD CONSTRAINT strategy_pnl_tape_research_trial_id_fkey FOREIGN KEY (research_trial_id) REFERENCES analysis.research_trial(id);

ALTER TABLE ONLY analysis.strategy_pnl_tape
    ADD CONSTRAINT strategy_pnl_tape_strategy_forecast_id_fkey FOREIGN KEY (strategy_forecast_id) REFERENCES analysis.strategy_forecast(id);

ALTER TABLE ONLY analysis.strategy_pnl_tape
    ADD CONSTRAINT strategy_pnl_tape_strategy_revision_id_fkey FOREIGN KEY (strategy_revision_id) REFERENCES analysis.strategy_revision(id);

ALTER TABLE ONLY analysis.strategy_pnl_tape
    ADD CONSTRAINT strategy_pnl_tape_trial_result_id_fkey FOREIGN KEY (trial_result_id) REFERENCES analysis.trial_result(id);

ALTER TABLE ONLY analysis.strategy_revision
    ADD CONSTRAINT strategy_revision_experiment_family_id_fkey FOREIGN KEY (experiment_family_id) REFERENCES analysis.experiment_family(id);

ALTER TABLE ONLY analysis.strategy_revision
    ADD CONSTRAINT strategy_revision_hypothesis_id_fkey FOREIGN KEY (hypothesis_id) REFERENCES analysis.hypothesis(id);

ALTER TABLE ONLY analysis.strategy_revision
    ADD CONSTRAINT strategy_revision_supersedes_id_fkey FOREIGN KEY (supersedes_id) REFERENCES analysis.strategy_revision(id);

ALTER TABLE ONLY analysis.symbol_decision
    ADD CONSTRAINT symbol_decision_decision_id_fkey FOREIGN KEY (decision_id) REFERENCES analysis.decision(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.symbol_decision_outcome
    ADD CONSTRAINT symbol_decision_outcome_decision_id_fkey FOREIGN KEY (decision_id) REFERENCES analysis.decision(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.symbol_decision_outcome
    ADD CONSTRAINT symbol_decision_outcome_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.symbol_feature
    ADD CONSTRAINT symbol_feature_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY analysis.symbol_feature
    ADD CONSTRAINT symbol_feature_run_id_fkey FOREIGN KEY (run_id) REFERENCES analysis.run(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.ticker_data_request
    ADD CONSTRAINT ticker_data_request_ticker_decision_id_fkey FOREIGN KEY (ticker_decision_id) REFERENCES analysis.ticker_decision(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.ticker_decision
    ADD CONSTRAINT ticker_decision_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.ticker_decision
    ADD CONSTRAINT ticker_decision_market_state_publication_id_fkey FOREIGN KEY (market_state_publication_id) REFERENCES app.publication(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.ticker_input_manifest
    ADD CONSTRAINT ticker_input_manifest_ticker_decision_id_fkey FOREIGN KEY (ticker_decision_id) REFERENCES analysis.ticker_decision(id) ON DELETE CASCADE;

ALTER TABLE ONLY analysis.ticker_outcome
    ADD CONSTRAINT ticker_outcome_ticker_decision_id_fkey FOREIGN KEY (ticker_decision_id) REFERENCES analysis.ticker_decision(id) ON DELETE RESTRICT;

ALTER TABLE ONLY analysis.trial_result
    ADD CONSTRAINT trial_result_research_trial_id_fkey FOREIGN KEY (research_trial_id) REFERENCES analysis.research_trial(id);

ALTER TABLE ONLY analysis.trial_universe_manifest
    ADD CONSTRAINT trial_universe_manifest_research_trial_id_fkey FOREIGN KEY (research_trial_id) REFERENCES analysis.research_trial(id);

ALTER TABLE ONLY analysis.universe_observation
    ADD CONSTRAINT universe_observation_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY analysis.universe_observation
    ADD CONSTRAINT universe_observation_research_trial_id_fkey FOREIGN KEY (research_trial_id) REFERENCES analysis.research_trial(id);

ALTER TABLE ONLY analysis.validation_dossier
    ADD CONSTRAINT validation_dossier_research_trial_id_fkey FOREIGN KEY (research_trial_id) REFERENCES analysis.research_trial(id);

ALTER TABLE ONLY analysis.validation_dossier
    ADD CONSTRAINT validation_dossier_strategy_revision_id_fkey FOREIGN KEY (strategy_revision_id) REFERENCES analysis.strategy_revision(id);

ALTER TABLE ONLY analysis.validation_gate_result
    ADD CONSTRAINT validation_gate_result_dossier_id_fkey FOREIGN KEY (dossier_id) REFERENCES analysis.validation_dossier(id);

ALTER TABLE ONLY app.alert
    ADD CONSTRAINT alert_decision_id_fkey FOREIGN KEY (decision_id) REFERENCES analysis.decision(id);

ALTER TABLE ONLY app.alert
    ADD CONSTRAINT alert_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY app.catalyst
    ADD CONSTRAINT catalyst_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY app.catalyst
    ADD CONSTRAINT catalyst_market_event_id_fkey FOREIGN KEY (market_event_id) REFERENCES raw.market_event(id);

ALTER TABLE ONLY app.current_publication_item
    ADD CONSTRAINT current_publication_item_content_hash_fkey FOREIGN KEY (content_hash) REFERENCES app.publication_payload(content_hash) ON DELETE RESTRICT;

ALTER TABLE ONLY app.current_publication_item
    ADD CONSTRAINT current_publication_item_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY app.current_publication_item
    ADD CONSTRAINT current_publication_item_publication_id_fkey FOREIGN KEY (publication_id) REFERENCES app.publication(id) ON DELETE CASCADE;

ALTER TABLE ONLY app.decision_inbox_item
    ADD CONSTRAINT decision_inbox_item_paper_order_id_fkey FOREIGN KEY (paper_order_id) REFERENCES app.paper_order(id) ON DELETE RESTRICT;

ALTER TABLE ONLY app.decision_truth
    ADD CONSTRAINT decision_truth_event_id_fkey FOREIGN KEY (event_id) REFERENCES analysis.event_decision_packet(event_id) ON DELETE SET NULL;

ALTER TABLE ONLY app.catalyst
    ADD CONSTRAINT fk_app_catalyst_supersedes FOREIGN KEY (supersedes_id) REFERENCES app.catalyst(id);

ALTER TABLE ONLY app.option_history_policy
    ADD CONSTRAINT fk_option_history_policy_event FOREIGN KEY (event_id) REFERENCES analysis.option_event(id) ON DELETE SET NULL;

ALTER TABLE ONLY app.notification_outbox
    ADD CONSTRAINT notification_outbox_inbox_item_id_fkey FOREIGN KEY (inbox_item_id) REFERENCES app.decision_inbox_item(id) ON DELETE RESTRICT;

ALTER TABLE ONLY app.option_history_policy
    ADD CONSTRAINT option_history_policy_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id) ON DELETE RESTRICT;

ALTER TABLE ONLY app.paper_execution_observation
    ADD CONSTRAINT paper_execution_observation_allocation_item_id_fkey FOREIGN KEY (allocation_item_id) REFERENCES analysis.portfolio_allocation_item(allocation_item_id);

ALTER TABLE ONLY app.paper_execution_observation
    ADD CONSTRAINT paper_execution_observation_paper_order_id_fkey FOREIGN KEY (paper_order_id) REFERENCES app.paper_order(id);

ALTER TABLE ONLY app.paper_order
    ADD CONSTRAINT paper_order_cohort_id_fkey FOREIGN KEY (cohort_id) REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT;

ALTER TABLE ONLY app.paper_order
    ADD CONSTRAINT paper_order_decision_id_fkey FOREIGN KEY (decision_id) REFERENCES analysis.decision(id);

ALTER TABLE ONLY app.paper_order
    ADD CONSTRAINT paper_order_event_id_fkey FOREIGN KEY (event_id) REFERENCES analysis.option_event(id) ON DELETE SET NULL;

ALTER TABLE ONLY app.paper_order
    ADD CONSTRAINT paper_order_event_signal_id_fkey FOREIGN KEY (event_signal_id) REFERENCES analysis.option_event_signal(id) ON DELETE SET NULL;

ALTER TABLE ONLY app.paper_order
    ADD CONSTRAINT paper_order_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY app.paper_order_leg
    ADD CONSTRAINT paper_order_leg_paper_order_id_fkey FOREIGN KEY (paper_order_id) REFERENCES app.paper_order(id) ON DELETE CASCADE;

ALTER TABLE ONLY app.paper_order
    ADD CONSTRAINT paper_order_ticker_decision_id_fkey FOREIGN KEY (ticker_decision_id) REFERENCES analysis.ticker_decision(id) ON DELETE RESTRICT;

ALTER TABLE ONLY app.portfolio_position
    ADD CONSTRAINT portfolio_position_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY app.portfolio_transaction
    ADD CONSTRAINT portfolio_transaction_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY app.portfolio_transaction
    ADD CONSTRAINT portfolio_transaction_reverses_transaction_id_fkey FOREIGN KEY (reverses_transaction_id) REFERENCES app.portfolio_transaction(id);

ALTER TABLE ONLY app.publication
    ADD CONSTRAINT publication_analysis_run_id_fkey FOREIGN KEY (analysis_run_id) REFERENCES analysis.run(id);

ALTER TABLE ONLY app.publication
    ADD CONSTRAINT publication_bundle_id_fkey FOREIGN KEY (bundle_id) REFERENCES app.publication_bundle(id) ON DELETE RESTRICT;

ALTER TABLE ONLY app.publication_bundle_item
    ADD CONSTRAINT publication_bundle_item_bundle_id_fkey FOREIGN KEY (bundle_id) REFERENCES app.publication_bundle(id) ON DELETE CASCADE;

ALTER TABLE ONLY app.publication_bundle_item
    ADD CONSTRAINT publication_bundle_item_content_hash_fkey FOREIGN KEY (content_hash) REFERENCES app.publication_payload(content_hash) ON DELETE RESTRICT;

ALTER TABLE ONLY app.publication_bundle_item
    ADD CONSTRAINT publication_bundle_item_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY app.publication_item
    ADD CONSTRAINT publication_item_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY app.publication_item
    ADD CONSTRAINT publication_item_publication_id_fkey FOREIGN KEY (publication_id) REFERENCES app.publication(id) ON DELETE CASCADE;

ALTER TABLE ONLY app.research_report
    ADD CONSTRAINT research_report_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY app.thesis
    ADD CONSTRAINT thesis_automation_run_id_fkey FOREIGN KEY (automation_run_id) REFERENCES app.thesis_automation_run(id);

ALTER TABLE ONLY app.thesis_automation_run
    ADD CONSTRAINT thesis_automation_run_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY app.thesis_evidence_assessment
    ADD CONSTRAINT thesis_evidence_assessment_automation_run_id_fkey FOREIGN KEY (automation_run_id) REFERENCES app.thesis_automation_run(id);

ALTER TABLE ONLY app.thesis_evidence_assessment
    ADD CONSTRAINT thesis_evidence_assessment_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY app.thesis_evidence_assessment
    ADD CONSTRAINT thesis_evidence_assessment_thesis_revision_id_fkey FOREIGN KEY (thesis_revision_id) REFERENCES app.thesis(id) ON DELETE CASCADE;

ALTER TABLE ONLY app.thesis_expression
    ADD CONSTRAINT thesis_expression_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY app.thesis_expression
    ADD CONSTRAINT thesis_expression_thesis_revision_id_fkey FOREIGN KEY (thesis_revision_id) REFERENCES app.thesis(id) ON DELETE CASCADE;

ALTER TABLE ONLY app.thesis
    ADD CONSTRAINT thesis_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY app.thesis_review_event
    ADD CONSTRAINT thesis_review_event_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY app.thesis_review_event
    ADD CONSTRAINT thesis_review_event_thesis_revision_id_fkey FOREIGN KEY (thesis_revision_id) REFERENCES app.thesis(id);

ALTER TABLE ONLY app.thesis
    ADD CONSTRAINT thesis_source_agent_task_id_fkey FOREIGN KEY (source_agent_task_id) REFERENCES analysis.agent_task(id);

ALTER TABLE ONLY app.thesis
    ADD CONSTRAINT thesis_superseded_revision_id_fkey FOREIGN KEY (superseded_revision_id) REFERENCES app.thesis(id);

ALTER TABLE ONLY app.trade_journal
    ADD CONSTRAINT trade_journal_decision_id_fkey FOREIGN KEY (decision_id) REFERENCES analysis.decision(id);

ALTER TABLE ONLY app.trade_journal
    ADD CONSTRAINT trade_journal_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY app.watchlist_item
    ADD CONSTRAINT watchlist_item_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY catalog.instrument_alias
    ADD CONSTRAINT instrument_alias_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id) ON DELETE CASCADE;

ALTER TABLE ONLY catalog.option_contract
    ADD CONSTRAINT option_contract_underlying_instrument_id_fkey FOREIGN KEY (underlying_instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY ingest.payload
    ADD CONSTRAINT payload_run_id_fkey FOREIGN KEY (run_id) REFERENCES ingest.run(id) ON DELETE CASCADE;

ALTER TABLE ONLY ingest.run
    ADD CONSTRAINT run_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id);

ALTER TABLE ONLY ingest.source_lifecycle_history
    ADD CONSTRAINT source_lifecycle_history_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id);

ALTER TABLE ONLY ops.storage_archive_manifest_reference
    ADD CONSTRAINT storage_archive_manifest_reference_manifest_id_fkey FOREIGN KEY (manifest_id) REFERENCES ops.storage_archive_manifest(id) ON DELETE RESTRICT;

ALTER TABLE ONLY raw.broker_account_snapshot
    ADD CONSTRAINT broker_account_snapshot_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.broker_account_snapshot
    ADD CONSTRAINT broker_account_snapshot_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id);

ALTER TABLE ONLY raw.broker_activity
    ADD CONSTRAINT broker_activity_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.broker_activity
    ADD CONSTRAINT broker_activity_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY raw.broker_activity
    ADD CONSTRAINT broker_activity_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id);

ALTER TABLE ONLY raw.broker_position_snapshot
    ADD CONSTRAINT broker_position_snapshot_account_snapshot_id_fkey FOREIGN KEY (account_snapshot_id) REFERENCES raw.broker_account_snapshot(id) ON DELETE CASCADE;

ALTER TABLE ONLY raw.broker_position_snapshot
    ADD CONSTRAINT broker_position_snapshot_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY raw.content_item
    ADD CONSTRAINT content_item_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.content_item_instrument
    ADD CONSTRAINT content_item_instrument_content_item_id_fkey FOREIGN KEY (content_item_id) REFERENCES raw.content_item(id) ON DELETE CASCADE;

ALTER TABLE ONLY raw.content_item_instrument
    ADD CONSTRAINT content_item_instrument_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY raw.content_item
    ADD CONSTRAINT content_item_payload_id_fkey FOREIGN KEY (payload_id) REFERENCES ingest.payload(id);

ALTER TABLE ONLY raw.content_item
    ADD CONSTRAINT content_item_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id);

ALTER TABLE ONLY raw.disclosure
    ADD CONSTRAINT disclosure_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.disclosure
    ADD CONSTRAINT disclosure_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY raw.disclosure
    ADD CONSTRAINT disclosure_payload_id_fkey FOREIGN KEY (payload_id) REFERENCES ingest.payload(id);

ALTER TABLE ONLY raw.disclosure
    ADD CONSTRAINT disclosure_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id);

ALTER TABLE ONLY raw.option_snapshot
    ADD CONSTRAINT fk_option_snapshot_latest_generation FOREIGN KEY (latest_complete_generation_id) REFERENCES raw.option_capture_generation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY raw.fundamental_observation
    ADD CONSTRAINT fundamental_observation_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.fundamental_observation
    ADD CONSTRAINT fundamental_observation_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY raw.fundamental_observation
    ADD CONSTRAINT fundamental_observation_payload_id_fkey FOREIGN KEY (payload_id) REFERENCES ingest.payload(id);

ALTER TABLE ONLY raw.fundamental_observation
    ADD CONSTRAINT fundamental_observation_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id);

ALTER TABLE ONLY raw.market_event
    ADD CONSTRAINT market_event_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.market_event
    ADD CONSTRAINT market_event_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY raw.market_event
    ADD CONSTRAINT market_event_payload_id_fkey FOREIGN KEY (payload_id) REFERENCES ingest.payload(id);

ALTER TABLE ONLY raw.market_event
    ADD CONSTRAINT market_event_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id);

ALTER TABLE ONLY raw.market_event_version
    ADD CONSTRAINT market_event_version_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.market_event_version
    ADD CONSTRAINT market_event_version_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY raw.market_event_version
    ADD CONSTRAINT market_event_version_market_event_id_fkey FOREIGN KEY (market_event_id) REFERENCES raw.market_event(id) ON DELETE CASCADE;

ALTER TABLE ONLY raw.market_event_version
    ADD CONSTRAINT market_event_version_payload_id_fkey FOREIGN KEY (payload_id) REFERENCES ingest.payload(id);

ALTER TABLE ONLY raw.market_event_version
    ADD CONSTRAINT market_event_version_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id);

ALTER TABLE ONLY raw.market_observation
    ADD CONSTRAINT market_observation_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.market_observation
    ADD CONSTRAINT market_observation_payload_id_fkey FOREIGN KEY (payload_id) REFERENCES ingest.payload(id);

ALTER TABLE ONLY raw.market_observation
    ADD CONSTRAINT market_observation_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id);

ALTER TABLE ONLY raw.option_capture_generation
    ADD CONSTRAINT option_capture_generation_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.option_capture_generation
    ADD CONSTRAINT option_capture_generation_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES raw.option_snapshot(id) ON DELETE RESTRICT;

ALTER TABLE raw.option_quote
    ADD CONSTRAINT option_quote_capture_generation_id_fkey FOREIGN KEY (capture_generation_id) REFERENCES raw.option_capture_generation(id) ON DELETE RESTRICT;

ALTER TABLE raw.option_quote
    ADD CONSTRAINT option_quote_contract_id_fkey FOREIGN KEY (contract_id) REFERENCES catalog.option_contract(id);

ALTER TABLE raw.option_quote
    ADD CONSTRAINT option_quote_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES raw.option_snapshot(id) ON DELETE CASCADE;

ALTER TABLE ONLY raw.option_snapshot
    ADD CONSTRAINT option_snapshot_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.option_snapshot
    ADD CONSTRAINT option_snapshot_payload_id_fkey FOREIGN KEY (payload_id) REFERENCES ingest.payload(id);

ALTER TABLE ONLY raw.option_snapshot
    ADD CONSTRAINT option_snapshot_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id);

ALTER TABLE ONLY raw.price_bar_confirmation
    ADD CONSTRAINT price_bar_confirmation_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.price_bar_fact_availability
    ADD CONSTRAINT price_bar_fact_availability_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.price_bar
    ADD CONSTRAINT price_bar_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.price_bar
    ADD CONSTRAINT price_bar_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY raw.price_bar
    ADD CONSTRAINT price_bar_payload_id_fkey FOREIGN KEY (payload_id) REFERENCES ingest.payload(id);

ALTER TABLE ONLY raw.price_bar
    ADD CONSTRAINT price_bar_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id);

ALTER TABLE ONLY raw.quote_confirmation
    ADD CONSTRAINT quote_confirmation_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.quote_fact_availability
    ADD CONSTRAINT quote_fact_availability_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.quote
    ADD CONSTRAINT quote_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id);

ALTER TABLE ONLY raw.quote
    ADD CONSTRAINT quote_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id);

ALTER TABLE ONLY raw.quote
    ADD CONSTRAINT quote_payload_id_fkey FOREIGN KEY (payload_id) REFERENCES ingest.payload(id);

ALTER TABLE ONLY raw.quote
    ADD CONSTRAINT quote_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id);

GRANT USAGE ON SCHEMA analysis TO market_research_signer;
GRANT USAGE ON SCHEMA analysis TO market_app;

GRANT USAGE ON SCHEMA app TO market_app;

GRANT USAGE ON SCHEMA catalog TO market_research_signer;
GRANT USAGE ON SCHEMA catalog TO market_app;

GRANT USAGE ON SCHEMA ingest TO market_app;
GRANT USAGE ON SCHEMA ingest TO market_migrator;

GRANT USAGE ON SCHEMA ops TO market_app;

GRANT USAGE ON SCHEMA public TO market_research_signer;

GRANT USAGE ON SCHEMA raw TO market_app;

REVOKE ALL ON FUNCTION analysis.insert_phase4_allocation_item(p jsonb) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.insert_phase4_allocation_snapshot(p jsonb) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.insert_phase4_book_attribution(p jsonb) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.insert_phase4_execution(p_id text, p_allocation_id text, p_model_version text, p_calibration_status text, p_sample_count integer, p_fill_probability double precision, p_spread_bps double precision, p_latency_ms double precision, p_impact_bps double precision, p_input_cutoff timestamp with time zone, p_input_hash text, p_content_hash text, p_metadata jsonb) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.insert_phase4_paper_execution_observation(p jsonb) FROM PUBLIC;

GRANT ALL ON FUNCTION analysis.insert_phase4_scenario(p_id text, p_allocation_id text, p_model_version text, p_probability_semantics text, p_scenarios jsonb, p_tail_dependence jsonb, p_simultaneous_unwind jsonb, p_input_cutoff timestamp with time zone, p_input_hash text, p_content_hash text) TO market_app;

REVOKE ALL ON FUNCTION analysis.phase4_allocation_authorization_payload(p_snapshot jsonb, p_items jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION analysis.phase4_allocation_authorization_payload(p_snapshot jsonb, p_items jsonb) TO market_app;

REVOKE ALL ON FUNCTION analysis.phase4_allocation_signing_key() FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.phase4_telemetry_authorization_payload(p_contract text, p_payload jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION analysis.phase4_telemetry_authorization_payload(p_contract text, p_payload jsonb) TO market_app;

REVOKE ALL ON FUNCTION analysis.phase4_telemetry_authorized(p_contract text, p_payload jsonb, p_signature text) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.promote_phase3_strategy(revision_id bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION analysis.promote_phase3_strategy(revision_id bigint) TO market_app;

REVOKE ALL ON FUNCTION analysis.research_evaluator_authorization_payload(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION analysis.research_evaluator_authorization_payload(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb) TO market_app;

REVOKE ALL ON FUNCTION analysis.research_evaluator_output_hash_v2(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb, available timestamp with time zone) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.research_evaluator_signature_payload(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output_digest text, available timestamp with time zone) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.research_evaluator_signing_key() FROM PUBLIC;

GRANT ALL ON FUNCTION analysis.research_evidence_complete(result_uuid uuid) TO market_app;

REVOKE ALL ON FUNCTION analysis.research_validation_evidence_complete(result_uuid uuid, expected_attempt_count integer) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.write_phase4_allocation(p_snapshot jsonb, p_items jsonb, p_authorization_signature text) FROM PUBLIC;
GRANT ALL ON FUNCTION analysis.write_phase4_allocation(p_snapshot jsonb, p_items jsonb, p_authorization_signature text) TO market_app;

REVOKE ALL ON FUNCTION analysis.write_phase4_book_attribution(p jsonb, sig text) FROM PUBLIC;
GRANT ALL ON FUNCTION analysis.write_phase4_book_attribution(p jsonb, sig text) TO market_app;

REVOKE ALL ON FUNCTION analysis.write_phase4_execution(p jsonb, sig text) FROM PUBLIC;
GRANT ALL ON FUNCTION analysis.write_phase4_execution(p jsonb, sig text) TO market_app;

REVOKE ALL ON FUNCTION analysis.write_phase4_execution_0077(p jsonb, sig text) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.write_phase4_paper_execution(p jsonb, sig text) FROM PUBLIC;
GRANT ALL ON FUNCTION analysis.write_phase4_paper_execution(p jsonb, sig text) TO market_app;

REVOKE ALL ON FUNCTION analysis.write_research_evaluator_output(p_trial_id uuid, p_result_id uuid, p_run_id uuid, p_kind text, p_evaluator text, p_code_version text, p_input_digest text, p_universe_digest text, p_feature_digest text, p_samples integer, p_valid boolean, p_output jsonb, p_authorization_signature text) FROM PUBLIC;
GRANT ALL ON FUNCTION analysis.write_research_evaluator_output(p_trial_id uuid, p_result_id uuid, p_run_id uuid, p_kind text, p_evaluator text, p_code_version text, p_input_digest text, p_universe_digest text, p_feature_digest text, p_samples integer, p_valid boolean, p_output jsonb, p_authorization_signature text) TO market_app;

REVOKE ALL ON FUNCTION ingest.record_source_lifecycle() FROM PUBLIC;

REVOKE ALL ON FUNCTION ingest.reject_identity_update() FROM PUBLIC;

GRANT ALL ON FUNCTION raw.current_price_at(p_as_of timestamp with time zone, p_instrument_ids bigint[]) TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.agent_experiment TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.agent_run TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.agent_task TO market_app;

GRANT SELECT ON TABLE analysis.book_attribution TO market_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE analysis.decision TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.decision_evidence TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.event_decision_packet TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.event_scout_event TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.event_study_feature TO market_app;

GRANT SELECT ON TABLE analysis.execution_model_snapshot TO market_app;

GRANT SELECT ON TABLE analysis.experiment_family TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE analysis.experiment_family TO market_app;

GRANT SELECT ON TABLE analysis.experiment_manifest TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE analysis.experiment_manifest TO market_app;

GRANT SELECT ON TABLE analysis.hypothesis TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE analysis.hypothesis TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.market_coverage_vector TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.market_scenario_path TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.market_state_posterior TO market_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE analysis.option_decision TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_discovery_candidate TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_discovery_run TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_event TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_event_agent_batch TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_event_capture TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_event_contract TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_event_detector_run TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_event_signal TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_event_spot TO market_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE analysis.option_feature TO market_app;

GRANT SELECT,USAGE ON SEQUENCE analysis.option_feature_id_seq TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_gate_result TO market_app;

GRANT SELECT ON TABLE analysis.option_history_anomaly TO market_app;

GRANT SELECT ON TABLE analysis.option_history_canary TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.option_liquidity_sla TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_opportunity_observation TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_outcome TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_recovery_cohort TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_recovery_event_session_quality TO market_app;

GRANT SELECT ON TABLE analysis.option_recovery_program_session TO market_app;

GRANT SELECT ON TABLE analysis.option_relative_value TO market_app;

GRANT SELECT ON TABLE analysis.option_relative_value_verification TO market_app;

GRANT SELECT ON TABLE analysis.option_surface_shift TO market_app;

GRANT SELECT ON TABLE analysis.option_surface_summary TO market_app;

GRANT SELECT ON TABLE analysis.portfolio_allocation_item TO market_app;

GRANT SELECT ON TABLE analysis.portfolio_allocation_snapshot TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.portfolio_drift_evidence TO market_app;

GRANT SELECT ON TABLE analysis.probabilistic_portfolio_scenario_artifact TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.reject_summary TO market_app;

GRANT SELECT,USAGE ON SEQUENCE analysis.reject_summary_id_seq TO market_app;

GRANT SELECT ON TABLE analysis.research_evidence_manifest TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE analysis.research_evidence_manifest TO market_app;

GRANT SELECT ON TABLE analysis.research_trial TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE analysis.research_trial TO market_app;

GRANT SELECT ON TABLE analysis.run TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE analysis.run TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.shadow_trade TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.source_signal TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.strategy_comparison TO market_app;

GRANT SELECT ON TABLE analysis.strategy_evaluation TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE analysis.strategy_evaluation TO market_app;

GRANT SELECT ON TABLE analysis.strategy_forecast TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE analysis.strategy_forecast TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.strategy_manifest TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.strategy_monitoring_evidence TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.strategy_pnl_tape TO market_app;

GRANT SELECT ON TABLE analysis.strategy_revision TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE analysis.strategy_revision TO market_app;

GRANT SELECT ON TABLE analysis.strategy_registry TO market_app;

GRANT SELECT ON TABLE analysis.trial_result TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE analysis.trial_result TO market_app;

GRANT SELECT ON TABLE analysis.trial_universe_manifest TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE analysis.trial_universe_manifest TO market_app;

GRANT SELECT ON TABLE analysis.universe_observation TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE analysis.universe_observation TO market_app;

GRANT SELECT ON TABLE analysis.strategy_trial_accounting TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.symbol_decision_outcome TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.symbol_feature TO market_app;

GRANT SELECT,USAGE ON SEQUENCE analysis.symbol_feature_id_seq TO market_app;

GRANT SELECT ON TABLE analysis.ticker_benchmark_snapshot TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE analysis.ticker_benchmark_snapshot TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.ticker_data_request TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.ticker_decision TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.ticker_input_manifest TO market_app;

GRANT SELECT,USAGE ON SEQUENCE analysis.ticker_input_manifest_id_seq TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.ticker_outcome TO market_app;

GRANT SELECT ON TABLE analysis.validation_dossier TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE analysis.validation_dossier TO market_app;

GRANT SELECT ON TABLE analysis.validation_gate_result TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE analysis.validation_gate_result TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.alert TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.catalyst TO market_app;

GRANT SELECT,INSERT,DELETE ON TABLE app.current_publication_item TO market_app;

GRANT SELECT ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(dedupe_key) ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(event_type) ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(opportunity_id) ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(ticket_version) ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(paper_order_id) ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(lane) ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(severity) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(status) ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(payload) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(resolved_at) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(user_state) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(snoozed_until) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(dismiss_reason) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(user_state_updated_at) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(reviewed_at) ON TABLE app.decision_inbox_item TO market_app;

GRANT SELECT,INSERT ON TABLE app.decision_inbox_sync_state TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.decision_truth TO market_app;

GRANT SELECT,INSERT ON TABLE app.manual_account_snapshot TO market_app;

GRANT SELECT,USAGE ON SEQUENCE app.manual_account_snapshot_id_seq TO market_app;

GRANT SELECT ON TABLE app.notification_outbox TO market_app;

GRANT INSERT(dedupe_key) ON TABLE app.notification_outbox TO market_app;

GRANT INSERT(inbox_item_id) ON TABLE app.notification_outbox TO market_app;

GRANT INSERT(event_type) ON TABLE app.notification_outbox TO market_app;

GRANT INSERT(payload) ON TABLE app.notification_outbox TO market_app;

GRANT UPDATE(status) ON TABLE app.notification_outbox TO market_app;

GRANT UPDATE(attempts) ON TABLE app.notification_outbox TO market_app;

GRANT UPDATE(next_attempt_at) ON TABLE app.notification_outbox TO market_app;

GRANT UPDATE(last_error) ON TABLE app.notification_outbox TO market_app;

GRANT UPDATE(sent_at) ON TABLE app.notification_outbox TO market_app;

GRANT UPDATE(updated_at) ON TABLE app.notification_outbox TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.option_history_policy TO market_app;

GRANT SELECT ON TABLE app.paper_execution_observation TO market_app;

GRANT SELECT ON TABLE app.paper_order TO market_app;

GRANT SELECT ON TABLE app.paper_order_leg TO market_app;

GRANT SELECT ON TABLE app.portfolio_position TO market_app;

GRANT SELECT ON TABLE app.portfolio_transaction TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.publication TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.publication_bundle TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.publication_bundle_item TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.publication_item TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.publication_payload TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.publication_content_item TO market_app;

GRANT SELECT ON TABLE app.setting TO market_app;

GRANT SELECT ON TABLE app.thesis TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.thesis_automation_run TO market_app;

GRANT SELECT ON TABLE app.thesis_evidence_assessment TO market_app;

GRANT SELECT ON TABLE app.thesis_expression TO market_app;

GRANT SELECT,INSERT ON TABLE app.thesis_review_event TO market_app;

GRANT SELECT,USAGE ON SEQUENCE app.thesis_review_event_id_seq TO market_app;

GRANT SELECT,INSERT ON TABLE app.trade_journal TO market_app;

GRANT SELECT ON TABLE app.watchlist_item TO market_app;

GRANT SELECT ON TABLE catalog.instrument TO market_research_signer;
GRANT SELECT,INSERT,UPDATE ON TABLE catalog.instrument TO market_app;

GRANT SELECT ON TABLE catalog.instrument_alias TO market_app;

GRANT SELECT,USAGE ON SEQUENCE catalog.instrument_alias_id_seq TO market_app;

GRANT SELECT,USAGE ON SEQUENCE catalog.instrument_id_seq TO market_app;

GRANT SELECT ON TABLE catalog.option_contract TO market_app;

GRANT SELECT,USAGE ON SEQUENCE catalog.option_contract_id_seq TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE ingest.payload TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE ingest.run TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE ingest.source TO market_app;

GRANT INSERT ON TABLE ingest.source_lifecycle_history TO market_migrator;
GRANT SELECT ON TABLE ingest.source_lifecycle_history TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE ops.job_run TO market_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE ops.provider_lease TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE ops.storage_archive_checkpoint TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE ops.storage_archive_manifest TO market_app;

GRANT SELECT,USAGE ON SEQUENCE ops.storage_archive_manifest_id_seq TO market_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE ops.storage_archive_manifest_reference TO market_app;

GRANT SELECT ON TABLE raw.broker_account_snapshot TO market_app;

GRANT SELECT ON TABLE raw.broker_position_snapshot TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.price_bar TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.price_bar_fact_availability TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.price_bar_history TO market_app;

GRANT SELECT ON TABLE raw.confirmed_price_bar TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.quote TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.quote_fact_availability TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.quote_history TO market_app;

GRANT SELECT ON TABLE raw.confirmed_quote TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.content_item TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.content_item_instrument TO market_app;

GRANT SELECT ON TABLE raw.disclosure TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.fundamental_observation TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.market_event TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.market_event_version TO market_app;

GRANT SELECT,INSERT ON TABLE raw.market_observation TO market_app;

GRANT SELECT ON TABLE raw.option_capture_generation TO market_app;

GRANT SELECT ON TABLE raw.option_quote TO market_app;

GRANT SELECT ON TABLE raw.option_snapshot TO market_app;

GRANT SELECT,INSERT,DELETE ON TABLE raw.price_bar_confirmation TO market_app;

GRANT SELECT,INSERT,DELETE ON TABLE raw.quote_confirmation TO market_app;
