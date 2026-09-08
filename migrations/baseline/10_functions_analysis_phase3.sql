-- Analysis functions: phase 3 and shared clocks.

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
