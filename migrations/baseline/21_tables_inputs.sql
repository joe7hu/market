-- Tables: ingest, source, and initial analysis inputs.

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
    latency_ms integer,
    CONSTRAINT agent_run_pkey PRIMARY KEY (id),
    CONSTRAINT agent_run_experiment_id_fkey FOREIGN KEY (experiment_id) REFERENCES analysis.agent_experiment(id) ON DELETE RESTRICT
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
    raw jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT event_scout_event_pkey PRIMARY KEY (event_id),
    CONSTRAINT event_scout_event_event_id_fkey FOREIGN KEY (event_id) REFERENCES analysis.event_decision_packet(event_id) ON DELETE CASCADE
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
    CONSTRAINT execution_model_snapshot_spread_bps_check CHECK (((spread_bps IS NULL) OR ((spread_bps < 'Infinity'::double precision) AND (spread_bps > '-Infinity'::double precision) AND (spread_bps >= (0)::double precision)))),
    CONSTRAINT execution_model_snapshot_pkey PRIMARY KEY (execution_model_snapshot_id),
    CONSTRAINT execution_model_snapshot_allocation_id_fkey FOREIGN KEY (allocation_id) REFERENCES analysis.portfolio_allocation_snapshot(allocation_id)
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
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT experiment_family_family_key_key UNIQUE (family_key),
    CONSTRAINT experiment_family_hypothesis_id_name_key UNIQUE (hypothesis_id, name),
    CONSTRAINT experiment_family_pkey PRIMARY KEY (id),
    CONSTRAINT experiment_family_hypothesis_id_fkey FOREIGN KEY (hypothesis_id) REFERENCES analysis.hypothesis(id)
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
    CONSTRAINT ck_option_event_type CHECK ((event_type = 'selloff'::text)),
    CONSTRAINT option_event_pkey PRIMARY KEY (id),
    CONSTRAINT option_event_cohort_id_fkey FOREIGN KEY (cohort_id) REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT,
    CONSTRAINT option_event_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id) ON DELETE RESTRICT
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
    computed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT option_recovery_program_session_cohort_id_trading_date_key UNIQUE (cohort_id, trading_date),
    CONSTRAINT option_recovery_program_session_pkey PRIMARY KEY (id),
    CONSTRAINT option_recovery_program_session_cohort_id_fkey FOREIGN KEY (cohort_id) REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT
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
    CONSTRAINT probabilistic_portfolio_scenario_artifact_tail_dependence_check CHECK (((jsonb_typeof(tail_dependence) = 'object'::text) AND (tail_dependence <> '{}'::jsonb))),
    CONSTRAINT probabilistic_portfolio_scenario_artifact_pkey PRIMARY KEY (scenario_artifact_id),
    CONSTRAINT probabilistic_portfolio_scenario_artifact_allocation_id_fkey FOREIGN KEY (allocation_id) REFERENCES analysis.portfolio_allocation_snapshot(allocation_id)
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
    CONSTRAINT ck_decision_truth_json_arrays CHECK (((jsonb_typeof(blockers) = 'array'::text) AND (jsonb_typeof(evidence_refs) = 'array'::text))),
    CONSTRAINT decision_truth_pkey PRIMARY KEY (symbol, lane),
    CONSTRAINT decision_truth_event_id_fkey FOREIGN KEY (event_id) REFERENCES analysis.event_decision_packet(event_id) ON DELETE SET NULL
);

CREATE TABLE app.portfolio_position (
    instrument_id bigint NOT NULL,
    quantity numeric(24,8) NOT NULL,
    average_cost numeric(20,6),
    purchase_date date,
    notes text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT portfolio_position_pkey PRIMARY KEY (instrument_id),
    CONSTRAINT portfolio_position_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id)
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
    CONSTRAINT ck_portfolio_transaction_type CHECK ((transaction_type = ANY (ARRAY['opening_balance'::text, 'buy'::text, 'sell'::text, 'dividend'::text, 'fee'::text, 'split'::text, 'transfer_in'::text, 'transfer_out'::text, 'cash_deposit'::text, 'cash_withdrawal'::text]))),
    CONSTRAINT portfolio_transaction_idempotency_key_key UNIQUE (idempotency_key),
    CONSTRAINT portfolio_transaction_pkey PRIMARY KEY (id),
    CONSTRAINT portfolio_transaction_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id),
    CONSTRAINT portfolio_transaction_reverses_transaction_id_fkey FOREIGN KEY (reverses_transaction_id) REFERENCES app.portfolio_transaction(id)
);

CREATE TABLE app.publication_bundle_item (
    bundle_id uuid NOT NULL,
    model_name text NOT NULL,
    stable_key text NOT NULL,
    rank integer NOT NULL,
    instrument_id bigint,
    content_hash character(64) NOT NULL,
    CONSTRAINT publication_bundle_item_pkey PRIMARY KEY (bundle_id, model_name, stable_key),
    CONSTRAINT publication_bundle_item_bundle_id_fkey FOREIGN KEY (bundle_id) REFERENCES app.publication_bundle(id) ON DELETE CASCADE,
    CONSTRAINT publication_bundle_item_content_hash_fkey FOREIGN KEY (content_hash) REFERENCES app.publication_payload(content_hash) ON DELETE RESTRICT,
    CONSTRAINT publication_bundle_item_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id)
);

CREATE TABLE app.research_report (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    instrument_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    report_type text NOT NULL,
    markdown text,
    report jsonb NOT NULL,
    evidence jsonb DEFAULT '[]'::jsonb NOT NULL,
    CONSTRAINT research_report_pkey PRIMARY KEY (id),
    CONSTRAINT research_report_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id)
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
    CONSTRAINT thesis_automation_run_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'succeeded'::text, 'failed'::text, 'timeout'::text, 'skipped'::text]))),
    CONSTRAINT thesis_automation_run_pkey PRIMARY KEY (id),
    CONSTRAINT thesis_automation_run_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id)
);

CREATE TABLE app.watchlist_item (
    instrument_id bigint NOT NULL,
    watch_state text NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT watchlist_item_pkey PRIMARY KEY (instrument_id),
    CONSTRAINT watchlist_item_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id)
);

CREATE TABLE catalog.instrument_alias (
    id bigint GENERATED BY DEFAULT AS IDENTITY (

    SEQUENCE NAME catalog.instrument_alias_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1

    ) NOT NULL,
    instrument_id bigint NOT NULL,
    provider text NOT NULL,
    external_symbol text NOT NULL,
    exchange text DEFAULT ''::text NOT NULL,
    currency text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT instrument_alias_pkey PRIMARY KEY (id),
    CONSTRAINT instrument_alias_provider_external_symbol_exchange_key UNIQUE (provider, external_symbol, exchange),
    CONSTRAINT instrument_alias_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id) ON DELETE CASCADE
);

CREATE TABLE catalog.option_contract (
    id bigint GENERATED BY DEFAULT AS IDENTITY (

    SEQUENCE NAME catalog.option_contract_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1

    ) NOT NULL,
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
    CONSTRAINT option_contract_option_type_check CHECK ((option_type = ANY (ARRAY['call'::text, 'put'::text]))),
    CONSTRAINT option_contract_pkey PRIMARY KEY (id),
    CONSTRAINT uq_option_contract_deliverable UNIQUE (underlying_instrument_id, expiration, strike, option_type, multiplier, deliverable_key),
    CONSTRAINT option_contract_underlying_instrument_id_fkey FOREIGN KEY (underlying_instrument_id) REFERENCES catalog.instrument(id)
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
    CONSTRAINT run_status_check CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'partial'::text, 'failed'::text, 'skipped'::text]))),
    CONSTRAINT run_pkey PRIMARY KEY (id),
    CONSTRAINT run_source_id_source_run_key_key UNIQUE (source_id, source_run_key),
    CONSTRAINT run_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id)
);

CREATE TABLE ingest.source_lifecycle_history (
    id bigint GENERATED BY DEFAULT AS IDENTITY (

    SEQUENCE NAME ingest.source_lifecycle_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1

    ) NOT NULL,
    source_id text NOT NULL,
    effective_at timestamp with time zone NOT NULL,
    enabled boolean NOT NULL,
    operational_state text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT source_lifecycle_history_pkey PRIMARY KEY (id),
    CONSTRAINT source_lifecycle_history_source_id_effective_at_enabled_ope_key UNIQUE (source_id, effective_at, enabled, operational_state),
    CONSTRAINT source_lifecycle_history_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id)
);

CREATE TABLE ops.storage_archive_manifest_reference (
    manifest_id bigint NOT NULL,
    source_relation text NOT NULL,
    source_row_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_ingest_run_id uuid,
    CONSTRAINT storage_archive_manifest_refe_source_relation_source_row_id_key UNIQUE (source_relation, source_row_id),
    CONSTRAINT storage_archive_manifest_reference_pkey PRIMARY KEY (manifest_id, source_relation, source_row_id),
    CONSTRAINT storage_archive_manifest_reference_manifest_id_fkey FOREIGN KEY (manifest_id) REFERENCES ops.storage_archive_manifest(id) ON DELETE RESTRICT
);

CREATE TABLE analysis.experiment_manifest (
    experiment_family_id uuid NOT NULL,
    expected_trial_count integer NOT NULL,
    expected_trial_keys jsonb NOT NULL,
    manifest_hash character(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT experiment_manifest_expected_trial_count_check CHECK (((expected_trial_count >= 1) AND (expected_trial_count <= 10000))),
    CONSTRAINT experiment_manifest_expected_trial_keys_check CHECK ((jsonb_typeof(expected_trial_keys) = 'array'::text)),
    CONSTRAINT experiment_manifest_pkey PRIMARY KEY (experiment_family_id),
    CONSTRAINT experiment_manifest_experiment_family_id_fkey FOREIGN KEY (experiment_family_id) REFERENCES analysis.experiment_family(id)
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
    CONSTRAINT ck_option_event_detector_run_status CHECK ((status = ANY (ARRAY['succeeded'::text, 'failed'::text, 'skipped'::text]))),
    CONSTRAINT option_event_detector_run_cohort_id_scheduled_at_key UNIQUE (cohort_id, scheduled_at),
    CONSTRAINT option_event_detector_run_pkey PRIMARY KEY (id),
    CONSTRAINT option_event_detector_run_cohort_id_fkey FOREIGN KEY (cohort_id) REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT,
    CONSTRAINT option_event_detector_run_provider_run_id_fkey FOREIGN KEY (provider_run_id) REFERENCES ingest.run(id) ON DELETE SET NULL
);

CREATE TABLE analysis.option_event_spot (
    event_id uuid NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    price double precision NOT NULL,
    source_id text,
    one_day_pct double precision,
    three_session_pct double precision,
    CONSTRAINT option_event_spot_pkey PRIMARY KEY (event_id, observed_at),
    CONSTRAINT option_event_spot_event_id_fkey FOREIGN KEY (event_id) REFERENCES analysis.option_event(id) ON DELETE CASCADE
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
    computed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT option_recovery_event_session_quality_event_id_trading_date_key UNIQUE (event_id, trading_date),
    CONSTRAINT option_recovery_event_session_quality_pkey PRIMARY KEY (id),
    CONSTRAINT option_recovery_event_session_quality_cohort_id_fkey FOREIGN KEY (cohort_id) REFERENCES analysis.option_recovery_cohort(id) ON DELETE RESTRICT,
    CONSTRAINT option_recovery_event_session_quality_event_id_fkey FOREIGN KEY (event_id) REFERENCES analysis.option_event(id) ON DELETE CASCADE
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
    CONSTRAINT research_trial_status_check CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'failed'::text, 'rejected'::text]))),
    CONSTRAINT research_trial_experiment_family_id_trial_key_key UNIQUE (experiment_family_id, trial_key),
    CONSTRAINT research_trial_pkey PRIMARY KEY (id),
    CONSTRAINT research_trial_experiment_family_id_fkey FOREIGN KEY (experiment_family_id) REFERENCES analysis.experiment_family(id)
);

CREATE TABLE analysis.strategy_revision (
    id bigint GENERATED BY DEFAULT AS IDENTITY (

    SEQUENCE NAME analysis.strategy_revision_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1

    ) NOT NULL,
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
    CONSTRAINT strategy_revision_promotability_check CHECK ((promotability = ANY (ARRAY['standard'::text, 'negative_control'::text, 'registration_only'::text, 'exposure_sleeve'::text]))),
    CONSTRAINT strategy_revision_pkey PRIMARY KEY (id),
    CONSTRAINT strategy_revision_strategy_key_revision_key UNIQUE (strategy_key, revision),
    CONSTRAINT strategy_revision_experiment_family_id_fkey FOREIGN KEY (experiment_family_id) REFERENCES analysis.experiment_family(id),
    CONSTRAINT strategy_revision_hypothesis_id_fkey FOREIGN KEY (hypothesis_id) REFERENCES analysis.hypothesis(id),
    CONSTRAINT strategy_revision_supersedes_id_fkey FOREIGN KEY (supersedes_id) REFERENCES analysis.strategy_revision(id)
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
    CONSTRAINT option_history_policy_hot_retention_days_check CHECK ((hot_retention_days >= 0)),
    CONSTRAINT option_history_policy_pkey PRIMARY KEY (instrument_id, profile),
    CONSTRAINT fk_option_history_policy_event FOREIGN KEY (event_id) REFERENCES analysis.option_event(id) ON DELETE SET NULL,
    CONSTRAINT option_history_policy_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id) ON DELETE RESTRICT
);

CREATE TABLE ingest.payload (
    id bigint GENERATED BY DEFAULT AS IDENTITY (

    SEQUENCE NAME ingest.payload_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1

    ) NOT NULL,
    run_id uuid NOT NULL,
    archive_uri text NOT NULL,
    sha256 character(64) NOT NULL,
    encoding text NOT NULL,
    byte_count bigint NOT NULL,
    schema_version text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT payload_byte_count_check CHECK ((byte_count >= 0)),
    CONSTRAINT payload_pkey PRIMARY KEY (id),
    CONSTRAINT payload_sha256_key UNIQUE (sha256),
    CONSTRAINT payload_run_id_fkey FOREIGN KEY (run_id) REFERENCES ingest.run(id) ON DELETE CASCADE
);

CREATE TABLE raw.broker_account_snapshot (
    id bigint GENERATED BY DEFAULT AS IDENTITY (

    SEQUENCE NAME raw.broker_account_snapshot_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1

    ) NOT NULL,
    source_id text NOT NULL,
    ingest_run_id uuid NOT NULL,
    account_key text NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    currency text,
    net_liquidation numeric(20,4),
    buying_power numeric(20,4),
    cash_balance numeric(20,4),
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT broker_account_snapshot_pkey PRIMARY KEY (id),
    CONSTRAINT broker_account_snapshot_source_id_account_key_observed_at_key UNIQUE (source_id, account_key, observed_at),
    CONSTRAINT broker_account_snapshot_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id),
    CONSTRAINT broker_account_snapshot_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id)
);

CREATE TABLE raw.broker_activity (
    id bigint GENERATED BY DEFAULT AS IDENTITY (

    SEQUENCE NAME raw.broker_activity_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1

    ) NOT NULL,
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
    CONSTRAINT broker_activity_activity_type_check CHECK ((activity_type = ANY (ARRAY['order'::text, 'fill'::text]))),
    CONSTRAINT broker_activity_pkey PRIMARY KEY (id),
    CONSTRAINT broker_activity_source_id_activity_key_activity_type_key UNIQUE (source_id, activity_key, activity_type),
    CONSTRAINT broker_activity_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id),
    CONSTRAINT broker_activity_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id),
    CONSTRAINT broker_activity_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id)
);

CREATE TABLE raw.option_capture_generation (
    id bigint GENERATED BY DEFAULT AS IDENTITY (

    SEQUENCE NAME raw.option_capture_generation_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1

    ) NOT NULL,
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
    CONSTRAINT option_capture_generation_completeness_check CHECK (((completeness >= (0)::double precision) AND (completeness <= (1)::double precision))),
    CONSTRAINT option_capture_generation_pkey PRIMARY KEY (id),
    CONSTRAINT option_capture_generation_snapshot_id_generation_key UNIQUE (snapshot_id, generation),
    CONSTRAINT option_capture_generation_snapshot_id_ingest_run_id_key UNIQUE (snapshot_id, ingest_run_id),
    CONSTRAINT option_capture_generation_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id)
);

CREATE TABLE raw.price_bar_confirmation (
    fact_id bigint NOT NULL,
    fact_available_at timestamp with time zone NOT NULL,
    ingest_run_id uuid NOT NULL,
    confirmed_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT price_bar_confirmation_pkey PRIMARY KEY (fact_id, fact_available_at, ingest_run_id),
    CONSTRAINT price_bar_confirmation_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id)
);

CREATE TABLE raw.price_bar_fact_availability (
    fact_id bigint NOT NULL,
    fact_available_at timestamp with time zone NOT NULL,
    ingest_run_id uuid NOT NULL,
    CONSTRAINT price_bar_fact_availability_pkey PRIMARY KEY (fact_id, fact_available_at),
    CONSTRAINT price_bar_fact_availability_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id)
);

CREATE TABLE raw.quote_confirmation (
    fact_id bigint NOT NULL,
    fact_available_at timestamp with time zone NOT NULL,
    ingest_run_id uuid NOT NULL,
    confirmed_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT quote_confirmation_pkey PRIMARY KEY (fact_id, fact_available_at, ingest_run_id),
    CONSTRAINT quote_confirmation_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id)
);

CREATE TABLE raw.quote_fact_availability (
    fact_id bigint NOT NULL,
    fact_available_at timestamp with time zone NOT NULL,
    ingest_run_id uuid NOT NULL,
    CONSTRAINT quote_fact_availability_pkey PRIMARY KEY (fact_id, fact_available_at),
    CONSTRAINT quote_fact_availability_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id)
);

CREATE TABLE raw.price_bar (
    id bigint GENERATED BY DEFAULT AS IDENTITY (

    SEQUENCE NAME raw.price_bar_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1

    ) CONSTRAINT price_bar_id_not_null NOT NULL,
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
    available_at timestamp with time zone DEFAULT clock_timestamp() CONSTRAINT price_bar_available_at_not_null NOT NULL,
    CONSTRAINT price_bar_instrument_id_source_id_interval_observed_at_key UNIQUE (instrument_id, source_id, "interval", observed_at),
    CONSTRAINT price_bar_pkey PRIMARY KEY (id),
    CONSTRAINT price_bar_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id),
    CONSTRAINT price_bar_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id),
    CONSTRAINT price_bar_payload_id_fkey FOREIGN KEY (payload_id) REFERENCES ingest.payload(id),
    CONSTRAINT price_bar_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id)
);

CREATE TABLE raw.quote (
    id bigint GENERATED BY DEFAULT AS IDENTITY (

    SEQUENCE NAME raw.quote_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1

    ) CONSTRAINT quote_id_not_null NOT NULL,
    instrument_id bigint CONSTRAINT quote_instrument_id_not_null NOT NULL,
    source_id text CONSTRAINT quote_source_id_not_null NOT NULL,
    ingest_run_id uuid CONSTRAINT quote_ingest_run_id_not_null NOT NULL,
    payload_id bigint,
    observed_at timestamp with time zone CONSTRAINT quote_observed_at_not_null NOT NULL,
    price double precision CONSTRAINT quote_price_not_null NOT NULL,
    change_abs double precision,
    change_pct double precision,
    currency text,
    available_at timestamp with time zone DEFAULT clock_timestamp() CONSTRAINT quote_available_at_not_null NOT NULL,
    CONSTRAINT quote_instrument_id_source_id_observed_at_key UNIQUE (instrument_id, source_id, observed_at),
    CONSTRAINT quote_pkey PRIMARY KEY (id),
    CONSTRAINT quote_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES ingest.run(id),
    CONSTRAINT quote_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id),
    CONSTRAINT quote_payload_id_fkey FOREIGN KEY (payload_id) REFERENCES ingest.payload(id),
    CONSTRAINT quote_source_id_fkey FOREIGN KEY (source_id) REFERENCES ingest.source(id)
);
