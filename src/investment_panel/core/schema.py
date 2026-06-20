"""DuckDB schema DDL (extracted from db.py).

Single source of truth for table/view definitions applied by
investment_panel.core.db.init_db.
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY,
    name TEXT,
    asset_class TEXT,
    sector TEXT,
    industry TEXT,
    category TEXT,
    source TEXT
);

CREATE TABLE IF NOT EXISTS prices_daily (
    symbol TEXT,
    date DATE,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume DOUBLE,
    source TEXT,
    PRIMARY KEY(symbol, date)
);

CREATE TABLE IF NOT EXISTS technical_features (
    symbol TEXT,
    date DATE,
    features JSON,
    PRIMARY KEY(symbol, date)
);

CREATE TABLE IF NOT EXISTS equity_fundamentals (
    symbol TEXT,
    period_end DATE,
    filing_date DATE,
    form_type TEXT,
    metrics JSON,
    source_url TEXT,
    PRIMARY KEY(symbol, period_end, form_type)
);

CREATE TABLE IF NOT EXISTS crypto_fundamentals (
    symbol TEXT,
    date DATE,
    metrics JSON,
    source TEXT,
    PRIMARY KEY(symbol, date)
);

CREATE TABLE IF NOT EXISTS disclosures (
    id TEXT PRIMARY KEY,
    source_type TEXT,
    trader_name TEXT,
    filer_name TEXT,
    symbol TEXT,
    event_date DATE,
    filed_date DATE,
    action TEXT,
    amount TEXT,
    raw JSON,
    source_url TEXT
);

CREATE TABLE IF NOT EXISTS birdclaw_theses (
    id TEXT PRIMARY KEY,
    symbol TEXT,
    author TEXT,
    created_at TIMESTAMP,
    thesis_summary TEXT,
    claims JSON,
    engagement JSON,
    source_url TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    run_date DATE,
    symbol TEXT,
    score DOUBLE,
    score_breakdown JSON,
    evidence JSON,
    decision TEXT
);

CREATE TABLE IF NOT EXISTS research_reports (
    id TEXT PRIMARY KEY,
    symbol TEXT,
    created_at TIMESTAMP,
    report_type TEXT,
    report_markdown TEXT,
    report_json JSON,
    evidence JSON
);

CREATE TABLE IF NOT EXISTS portfolio_positions (
    symbol TEXT PRIMARY KEY,
    quantity DOUBLE,
    avg_cost DOUBLE,
    purchase_date DATE,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS manual_watchlist (
    symbol TEXT PRIMARY KEY,
    name TEXT,
    asset_class TEXT,
    watch_state TEXT,
    notes TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS theses (
    symbol TEXT PRIMARY KEY,
    thesis_json JSON,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalysts (
    id TEXT PRIMARY KEY,
    symbol TEXT,
    event_date DATE,
    event TEXT,
    expected_impact TEXT,
    source TEXT,
    start_at TIMESTAMP,
    end_at TIMESTAMP,
    timezone TEXT,
    event_scope TEXT,
    event_kind TEXT,
    importance TEXT,
    verification_status TEXT,
    source_url TEXT,
    source_name TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY,
    checked_at TIMESTAMP,
    status TEXT,
    detail TEXT,
    source_url TEXT
);

CREATE TABLE IF NOT EXISTS source_registry (
    source_id TEXT PRIMARY KEY,
    source_name TEXT,
    source_family TEXT,
    source_kind TEXT,
    origin TEXT,
    enabled BOOLEAN,
    ingestion_mode TEXT,
    raw_access TEXT,
    source_url TEXT,
    notes TEXT,
    config JSON,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_runs (
    source_id TEXT,
    run_id TEXT,
    capability TEXT,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    status TEXT,
    item_count INTEGER,
    ticker_count INTEGER,
    failure_detail TEXT,
    raw JSON,
    PRIMARY KEY(source_id, run_id)
);

CREATE TABLE IF NOT EXISTS source_items (
    id TEXT PRIMARY KEY,
    source_id TEXT,
    source_run_id TEXT,
    source_kind TEXT,
    title TEXT,
    url TEXT,
    author TEXT,
    published_at TIMESTAMP,
    observed_at TIMESTAMP,
    summary TEXT,
    tickers JSON,
    evidence_refs JSON,
    raw JSON,
    content_hash TEXT,
    license_status TEXT
);

CREATE TABLE IF NOT EXISTS ticker_source_signals (
    id TEXT PRIMARY KEY,
    source_item_id TEXT,
    source_id TEXT,
    symbol TEXT,
    observed_at TIMESTAMP,
    signal_type TEXT,
    sentiment TEXT,
    direction TEXT,
    confidence DOUBLE,
    thesis TEXT,
    antithesis TEXT,
    catalysts JSON,
    risks JSON,
    invalidation TEXT,
    evidence_refs JSON,
    needs_market_context BOOLEAN,
    raw JSON
);

CREATE TABLE IF NOT EXISTS provider_runs (
    id TEXT PRIMARY KEY,
    provider TEXT,
    capability TEXT,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    status TEXT,
    detail TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS quotes_intraday (
    symbol TEXT,
    observed_at TIMESTAMP,
    price DOUBLE,
    change_pct DOUBLE,
    change_abs DOUBLE,
    currency TEXT,
    source TEXT,
    raw JSON,
    PRIMARY KEY(symbol, observed_at, source)
);

CREATE TABLE IF NOT EXISTS market_screener_rows (
    run_id TEXT,
    symbol TEXT,
    observed_at TIMESTAMP,
    name TEXT,
    metrics JSON,
    source TEXT,
    PRIMARY KEY(run_id, symbol)
);

CREATE TABLE IF NOT EXISTS options_expiries (
    symbol TEXT,
    expiry DATE,
    dte INTEGER,
    contracts_count INTEGER,
    observed_at TIMESTAMP,
    source TEXT,
    raw JSON,
    PRIMARY KEY(symbol, expiry, source)
);

CREATE TABLE IF NOT EXISTS options_chain (
    symbol TEXT,
    expiry DATE,
    strike DOUBLE,
    option_type TEXT,
    bid DOUBLE,
    ask DOUBLE,
    mid DOUBLE,
    iv DOUBLE,
    delta DOUBLE,
    gamma DOUBLE,
    theta DOUBLE,
    vega DOUBLE,
    rho DOUBLE,
    theo DOUBLE,
    bid_iv DOUBLE,
    ask_iv DOUBLE,
    contract_symbol TEXT,
    observed_at TIMESTAMP,
    source TEXT,
    raw JSON,
    PRIMARY KEY(symbol, expiry, strike, option_type, observed_at, source)
);

CREATE TABLE IF NOT EXISTS options_provider_capabilities (
    provider TEXT PRIMARY KEY,
    observed_at TIMESTAMP,
    supports_expiries BOOLEAN,
    supports_chain_quotes BOOLEAN,
    supports_greeks BOOLEAN,
    supports_theoretical_price BOOLEAN,
    supports_open_interest BOOLEAN,
    supports_volume BOOLEAN,
    supports_full_chain BOOLEAN,
    status TEXT,
    detail TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS options_expiry_signals (
    symbol TEXT,
    expiry DATE,
    as_of TIMESTAMP,
    source TEXT,
    dte INTEGER,
    spot DOUBLE,
    contract_count INTEGER,
    chain_rows INTEGER,
    atm_strike DOUBLE,
    atm_iv DOUBLE,
    expected_move DOUBLE,
    expected_move_pct DOUBLE,
    put_call_iv_skew DOUBLE,
    call_spread_pct DOUBLE,
    put_spread_pct DOUBLE,
    spread_quality TEXT,
    liquidity_score DOUBLE,
    hedge_put_strike DOUBLE,
    hedge_put_mid DOUBLE,
    covered_call_strike DOUBLE,
    covered_call_mid DOUBLE,
    unavailable_signals JSON,
    raw JSON,
    PRIMARY KEY(symbol, expiry, source)
);

CREATE TABLE IF NOT EXISTS options_ticker_signals (
    symbol TEXT,
    as_of TIMESTAMP,
    source TEXT,
    status TEXT,
    nearest_expiry DATE,
    nearest_dte INTEGER,
    atm_iv DOUBLE,
    iv_regime TEXT,
    expected_move DOUBLE,
    expected_move_pct DOUBLE,
    skew_signal TEXT,
    put_call_iv_skew DOUBLE,
    spread_quality TEXT,
    liquidity_score DOUBLE,
    hedge_summary TEXT,
    income_summary TEXT,
    unavailable_signals JSON,
    raw JSON,
    PRIMARY KEY(symbol, source)
);

CREATE TABLE IF NOT EXISTS option_strategy_versions (
    strategy_version TEXT PRIMARY KEY,
    strategy_name TEXT,
    version INTEGER,
    created_at TIMESTAMP,
    status TEXT,
    parameters JSON,
    promoted_at TIMESTAMP,
    supersedes TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS option_snapshot (
    snapshot_time TIMESTAMP,
    ticker TEXT,
    underlying_price DOUBLE,
    expiration DATE,
    strike DOUBLE,
    option_type TEXT,
    bid DOUBLE,
    ask DOUBLE,
    mid DOUBLE,
    last DOUBLE,
    volume DOUBLE,
    open_interest DOUBLE,
    iv DOUBLE,
    delta DOUBLE,
    gamma DOUBLE,
    theta DOUBLE,
    vega DOUBLE,
    dte INTEGER,
    spread_pct DOUBLE,
    data_source TEXT,
    contract_id TEXT,
    raw JSON,
    PRIMARY KEY(contract_id, snapshot_time, data_source)
);

CREATE TABLE IF NOT EXISTS option_features (
    snapshot_time TIMESTAMP,
    contract_id TEXT,
    ticker TEXT,
    required_2x_price DOUBLE,
    required_5x_price DOUBLE,
    required_10x_price DOUBLE,
    required_move_10x_pct DOUBLE,
    breakeven DOUBLE,
    iv_percentile DOUBLE,
    iv_rank DOUBLE,
    liquidity_score DOUBLE,
    convexity_score DOUBLE,
    raw JSON,
    PRIMARY KEY(contract_id, snapshot_time)
);

CREATE TABLE IF NOT EXISTS option_flow_features (
    snapshot_time TIMESTAMP,
    contract_id TEXT,
    ticker TEXT,
    oi_change_1d DOUBLE,
    oi_change_5d DOUBLE,
    oi_zscore_20d DOUBLE,
    volume_oi_ratio DOUBLE,
    volume_zscore_20d DOUBLE,
    ticker_call_oi_delta_1d DOUBLE,
    ticker_call_volume_premium_usd DOUBLE,
    flow_score DOUBLE,
    raw JSON,
    PRIMARY KEY(contract_id, snapshot_time)
);

CREATE TABLE IF NOT EXISTS trade_journal (
    journal_id TEXT PRIMARY KEY,
    created_at TIMESTAMP,
    strategy_version TEXT,
    ticker TEXT,
    contract_id TEXT,
    event_id TEXT,
    entry_premium DOUBLE,
    predicted_ev_multiple DOUBLE,
    predicted_p2x DOUBLE,
    conviction_score DOUBLE,
    opportunity_snapshot JSON,
    realized_return DOUBLE,
    realized_status TEXT,
    closed_at TIMESTAMP,
    notes TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS radar_alert (
    alert_id TEXT PRIMARY KEY,
    created_at TIMESTAMP,
    strategy_version TEXT,
    alert_type TEXT,
    ticker TEXT,
    contract_id TEXT,
    event_id TEXT,
    severity TEXT,
    message TEXT,
    title TEXT,
    detail TEXT,
    acknowledged_at TIMESTAMP,
    resolution_reason TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS conviction_calibration (
    strategy_version TEXT,
    bin_index INTEGER,
    bin_lo DOUBLE,
    bin_hi DOUBLE,
    n INTEGER,
    predicted_p2x DOUBLE,
    realized_p2x DOUBLE,
    realized_p5x DOUBLE,
    wilson_lo DOUBLE,
    wilson_hi DOUBLE,
    brier DOUBLE,
    mature_n INTEGER,
    calibrated BOOLEAN,
    as_of TIMESTAMP,
    raw JSON,
    PRIMARY KEY(strategy_version, bin_index)
);

CREATE TABLE IF NOT EXISTS vol_surface_features (
    snapshot_time TIMESTAMP,
    ticker TEXT,
    atm_iv_30d DOUBLE,
    atm_iv_90d DOUBLE,
    atm_iv_leap DOUBLE,
    term_slope DOUBLE,
    put_call_skew_25d DOUBLE,
    skew_change_5d DOUBLE,
    rv_20d DOUBLE,
    rv_60d DOUBLE,
    iv_rv_ratio DOUBLE,
    iv_percentile_252d DOUBLE,
    iv_percentile_basis TEXT,
    raw JSON,
    PRIMARY KEY(ticker, snapshot_time)
);

CREATE TABLE IF NOT EXISTS option_radar_opportunity (
    opportunity_id TEXT PRIMARY KEY,
    snapshot_time TIMESTAMP,
    ticker TEXT,
    strategy_version TEXT,
    tier TEXT,
    primary_event_id TEXT,
    primary_contract_id TEXT,
    primary_state TEXT,
    conviction_score DOUBLE,
    asymmetry_score DOUBLE,
    entry_quality_score DOUBLE,
    catalyst_score DOUBLE,
    evidence_score DOUBLE,
    regime_score DOUBLE,
    survivability_score DOUBLE,
    learning_score DOUBLE,
    required_move_pct DOUBLE,
    premium_mid DOUBLE,
    premium_fill_assumption DOUBLE,
    required_10x_price DOUBLE,
    buy_under DOUBLE,
    entry_zone TEXT,
    max_loss_assumption DOUBLE,
    position_sizing_band TEXT,
    data_contract_status TEXT,
    data_contract_failures JSON,
    data_contract_satisfied JSON,
    service_repair_jobs JSON,
    service_repair_summary TEXT,
    why_now TEXT,
    kill_switch TEXT,
    top_reasons JSON,
    blockers JSON,
    quality_status TEXT,
    quality_flags JSON,
    evidence_refs JSON,
    alternative_contracts JSON,
    raw JSON
);

CREATE TABLE IF NOT EXISTS stock_features (
    snapshot_time TIMESTAMP,
    ticker TEXT,
    price DOUBLE,
    ma_20 DOUBLE,
    ma_50 DOUBLE,
    ma_200 DOUBLE,
    rs_vs_qqq_20d DOUBLE,
    rs_vs_qqq_60d DOUBLE,
    atr_pct DOUBLE,
    volume_ratio DOUBLE,
    distance_from_52w_high DOUBLE,
    base_length_days INTEGER,
    breakout_level DOUBLE,
    raw JSON,
    PRIMARY KEY(ticker, snapshot_time)
);

CREATE TABLE IF NOT EXISTS agent_thesis (
    thesis_id TEXT PRIMARY KEY,
    ticker TEXT,
    created_at TIMESTAMP,
    agent_version TEXT,
    bull_target_price DOUBLE,
    bull_target_date DATE,
    base_target_price DOUBLE,
    core_thesis TEXT,
    required_proofs JSON,
    invalidation_conditions JSON,
    catalysts JSON,
    catalyst_summary TEXT,
    bear_case TEXT,
    confidence DOUBLE,
    evidence_refs JSON,
    raw JSON
);

CREATE TABLE IF NOT EXISTS agent_thesis_request (
    request_id TEXT PRIMARY KEY,
    created_at TIMESTAMP,
    ticker TEXT,
    event_id TEXT,
    strategy_version TEXT,
    priority_score DOUBLE,
    status TEXT,
    prompt TEXT,
    context JSON,
    raw JSON
);

CREATE TABLE IF NOT EXISTS agent_thesis_validation (
    validation_id TEXT PRIMARY KEY,
    thesis_id TEXT,
    ticker TEXT,
    strategy_version TEXT,
    validation_date DATE,
    candidate_event_id TEXT,
    candidate_snapshot_time TIMESTAMP,
    validated_at TIMESTAMP,
    state TEXT,
    reason TEXT,
    option_still_valid BOOLEAN,
    stock_progress TEXT,
    iv_status TEXT,
    candidate_state TEXT,
    proof_status TEXT,
    catalyst_status TEXT,
    invalidation_status TEXT,
    evidence_status TEXT,
    red_team_status TEXT,
    red_team_flags JSON,
    evidence_refs JSON,
    raw JSON
);

CREATE TABLE IF NOT EXISTS agent_postmortem_request (
    request_id TEXT PRIMARY KEY,
    created_at TIMESTAMP,
    source_type TEXT,
    source_id TEXT,
    ticker TEXT,
    strategy_version TEXT,
    priority_score DOUBLE,
    status TEXT,
    prompt TEXT,
    context JSON,
    raw JSON
);

CREATE TABLE IF NOT EXISTS agent_postmortem (
    postmortem_id TEXT PRIMARY KEY,
    request_id TEXT,
    source_type TEXT,
    source_id TEXT,
    created_at TIMESTAMP,
    agent_version TEXT,
    ticker TEXT,
    strategy_version TEXT,
    outcome_type TEXT,
    failure_type TEXT,
    evidence JSON,
    proposed_rule_change TEXT,
    proposed_parameter_changes JSON,
    expected_effect TEXT,
    risk TEXT,
    confidence DOUBLE,
    evidence_refs JSON,
    raw JSON
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    trigger TEXT,
    ticker TEXT,
    provider TEXT,
    model TEXT,
    input_tokens BIGINT,
    output_tokens BIGINT,
    tokens_estimated BOOLEAN,
    est_cost_usd DOUBLE,
    thesis_attempted INTEGER,
    thesis_accepted INTEGER,
    postmortem_attempted INTEGER,
    postmortem_accepted INTEGER,
    status TEXT,
    custom_prompt TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS candidate_event (
    event_id TEXT PRIMARY KEY,
    snapshot_time TIMESTAMP,
    ticker TEXT,
    contract_id TEXT,
    strategy_version TEXT,
    state TEXT,
    premium_mid DOUBLE,
    premium_fill_assumption DOUBLE,
    required_10x_price DOUBLE,
    required_move_pct DOUBLE,
    buy_under DOUBLE,
    trigger_reason TEXT,
    thesis_id TEXT,
    score DOUBLE,
    quality_status TEXT,
    quality_flags JSON,
    raw JSON
);

CREATE TABLE IF NOT EXISTS candidate_event_mark (
    mark_id TEXT PRIMARY KEY,
    event_id TEXT,
    contract_id TEXT,
    ticker TEXT,
    strategy_version TEXT,
    candidate_state TEXT,
    mark_time TIMESTAMP,
    alert_time TIMESTAMP,
    premium_fill_assumption DOUBLE,
    mark_price DOUBLE,
    current_return DOUBLE,
    return_1d DOUBLE,
    return_5d DOUBLE,
    return_20d DOUBLE,
    return_60d DOUBLE,
    max_return_since_alert DOUBLE,
    max_drawdown_since_alert DOUBLE,
    time_to_2x INTEGER,
    time_to_5x INTEGER,
    time_to_10x INTEGER,
    dte INTEGER,
    spread_pct DOUBLE,
    iv DOUBLE,
    underlying_price DOUBLE,
    raw JSON
);

CREATE TABLE IF NOT EXISTS candidate_event_attribution (
    attribution_id TEXT PRIMARY KEY,
    event_id TEXT,
    contract_id TEXT,
    ticker TEXT,
    strategy_version TEXT,
    candidate_state TEXT,
    snapshot_time TIMESTAMP,
    prior_snapshot_time TIMESTAMP,
    option_return DOUBLE,
    underlying_return DOUBLE,
    iv_change DOUBLE,
    theta_decay DOUBLE,
    spread_change DOUBLE,
    stock_move_effect DOUBLE,
    iv_effect DOUBLE,
    theta_effect DOUBLE,
    spread_effect DOUBLE,
    unexplained_effect DOUBLE,
    label TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS shadow_trade (
    trade_id TEXT PRIMARY KEY,
    event_id TEXT,
    entry_time TIMESTAMP,
    entry_price_assumption DOUBLE,
    exit_time TIMESTAMP,
    exit_price DOUBLE,
    status TEXT,
    max_return_seen DOUBLE,
    max_drawdown_seen DOUBLE,
    time_to_2x INTEGER,
    time_to_5x INTEGER,
    time_to_10x INTEGER,
    exit_reason TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS shadow_trade_mark (
    mark_id TEXT PRIMARY KEY,
    trade_id TEXT,
    event_id TEXT,
    contract_id TEXT,
    ticker TEXT,
    strategy_version TEXT,
    mark_time TIMESTAMP,
    entry_time TIMESTAMP,
    entry_price_assumption DOUBLE,
    mark_price DOUBLE,
    current_return DOUBLE,
    return_1d DOUBLE,
    return_5d DOUBLE,
    return_20d DOUBLE,
    return_60d DOUBLE,
    max_return_since_alert DOUBLE,
    max_drawdown_since_alert DOUBLE,
    time_to_2x INTEGER,
    time_to_5x INTEGER,
    time_to_10x INTEGER,
    dte INTEGER,
    spread_pct DOUBLE,
    iv DOUBLE,
    underlying_price DOUBLE,
    expired_worthless_probability_change DOUBLE,
    raw JSON
);

CREATE TABLE IF NOT EXISTS radar_state_transition (
    transition_id TEXT PRIMARY KEY,
    evaluated_at TIMESTAMP,
    snapshot_time TIMESTAMP,
    ticker TEXT,
    contract_id TEXT,
    strategy_version TEXT,
    previous_state TEXT,
    state TEXT,
    candidate_state TEXT,
    event_id TEXT,
    trade_id TEXT,
    mark_id TEXT,
    thesis_id TEXT,
    trigger_reason TEXT,
    evidence_refs JSON,
    raw JSON
);

CREATE TABLE IF NOT EXISTS option_attribution (
    attribution_id TEXT PRIMARY KEY,
    trade_id TEXT,
    event_id TEXT,
    contract_id TEXT,
    snapshot_time TIMESTAMP,
    prior_snapshot_time TIMESTAMP,
    option_return DOUBLE,
    underlying_return DOUBLE,
    iv_change DOUBLE,
    theta_decay DOUBLE,
    spread_change DOUBLE,
    stock_move_effect DOUBLE,
    iv_effect DOUBLE,
    theta_effect DOUBLE,
    spread_effect DOUBLE,
    unexplained_effect DOUBLE,
    label TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS missed_winner_event (
    missed_id TEXT PRIMARY KEY,
    detected_at TIMESTAMP,
    ticker TEXT,
    contract_id TEXT,
    strategy_version TEXT,
    first_snapshot_time TIMESTAMP,
    winner_snapshot_time TIMESTAMP,
    entry_price_assumption DOUBLE,
    winner_price DOUBLE,
    max_return_seen DOUBLE,
    winner_threshold TEXT,
    filter_reason TEXT,
    proposed_strategy_family TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS strategy_mutation_proposal (
    proposal_id TEXT PRIMARY KEY,
    created_at TIMESTAMP,
    source_type TEXT,
    strategy_version TEXT,
    proposed_strategy_version TEXT,
    proposed_parameter_changes JSON,
    rationale TEXT,
    expected_effect TEXT,
    risk TEXT,
    status TEXT,
    requires_backtest BOOLEAN,
    requires_forward_test BOOLEAN,
    human_approval_status TEXT,
    approved_by TEXT,
    approved_at TIMESTAMP,
    evidence_refs JSON,
    raw JSON
);

CREATE TABLE IF NOT EXISTS strategy_backtest_result (
    backtest_id TEXT PRIMARY KEY,
    proposal_id TEXT,
    evaluated_at TIMESTAMP,
    strategy_version TEXT,
    proposed_strategy_version TEXT,
    lookback_start TIMESTAMP,
    lookback_end TIMESTAMP,
    baseline_candidate_count INTEGER,
    proposed_candidate_count INTEGER,
    baseline_hit_rate_2x DOUBLE,
    baseline_hit_rate_5x DOUBLE,
    baseline_hit_rate_10x DOUBLE,
    proposed_hit_rate_2x DOUBLE,
    proposed_hit_rate_5x DOUBLE,
    proposed_hit_rate_10x DOUBLE,
    proposed_false_positive_rate DOUBLE,
    verdict TEXT,
    metrics JSON,
    raw JSON
);

CREATE TABLE IF NOT EXISTS strategy_forward_test_result (
    forward_test_id TEXT PRIMARY KEY,
    proposal_id TEXT,
    evaluated_at TIMESTAMP,
    strategy_version TEXT,
    proposed_strategy_version TEXT,
    forward_start TIMESTAMP,
    forward_end TIMESTAMP,
    days_observed INTEGER,
    baseline_candidate_count INTEGER,
    proposed_candidate_count INTEGER,
    baseline_hit_rate_2x DOUBLE,
    baseline_hit_rate_5x DOUBLE,
    baseline_hit_rate_10x DOUBLE,
    proposed_hit_rate_2x DOUBLE,
    proposed_hit_rate_5x DOUBLE,
    proposed_hit_rate_10x DOUBLE,
    status TEXT,
    verdict TEXT,
    metrics JSON,
    raw JSON
);

CREATE TABLE IF NOT EXISTS strategy_cohort_result (
    cohort_id TEXT PRIMARY KEY,
    evaluated_at TIMESTAMP,
    strategy_version TEXT,
    cohort_type TEXT,
    cohort_value TEXT,
    candidate_count INTEGER,
    hit_rate_2x DOUBLE,
    hit_rate_5x DOUBLE,
    hit_rate_10x DOUBLE,
    false_positive_rate DOUBLE,
    median_max_return DOUBLE,
    median_max_drawdown DOUBLE,
    average_time_to_2x DOUBLE,
    early_entry_rate DOUBLE,
    theta_iv_bleed_rate DOUBLE,
    good_convexity_rate DOUBLE,
    qqq_above_200d_rate DOUBLE,
    raw JSON
);

CREATE TABLE IF NOT EXISTS news_items (
    id TEXT PRIMARY KEY,
    published_at TIMESTAMP,
    provider TEXT,
    title TEXT,
    related_symbols JSON,
    link TEXT,
    source TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS tradingview_symbol_search (
    id TEXT PRIMARY KEY,
    query TEXT,
    observed_at TIMESTAMP,
    symbol TEXT,
    description TEXT,
    instrument_type TEXT,
    exchange TEXT,
    country TEXT,
    currency TEXT,
    source TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS instrument_market_identity (
    symbol TEXT PRIMARY KEY,
    primary_exchange TEXT,
    tradingview_symbol TEXT,
    provider TEXT,
    observed_at TIMESTAMP,
    source TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS tradingview_watchlists (
    id TEXT,
    observed_at TIMESTAMP,
    name TEXT,
    color TEXT,
    symbol_count INTEGER,
    symbols JSON,
    source TEXT,
    raw JSON,
    PRIMARY KEY(id, observed_at, source)
);

CREATE TABLE IF NOT EXISTS tradingview_alerts (
    id TEXT,
    observed_at TIMESTAMP,
    name TEXT,
    symbol TEXT,
    alert_type TEXT,
    condition TEXT,
    value DOUBLE,
    active BOOLEAN,
    status TEXT,
    fired_at TIMESTAMP,
    source TEXT,
    raw JSON,
    PRIMARY KEY(id, observed_at, source)
);

CREATE TABLE IF NOT EXISTS tradingview_chart_state (
    id TEXT PRIMARY KEY,
    observed_at TIMESTAMP,
    layout_id TEXT,
    symbol TEXT,
    interval TEXT,
    url TEXT,
    source TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS sepa_analyses (
    symbol TEXT,
    as_of DATE,
    score DOUBLE,
    stage TEXT,
    verdict TEXT,
    checklist JSON,
    metrics JSON,
    PRIMARY KEY(symbol, as_of)
);

CREATE TABLE IF NOT EXISTS liquidity_metrics (
    symbol TEXT,
    as_of DATE,
    grade TEXT,
    avg_daily_volume DOUBLE,
    avg_dollar_volume DOUBLE,
    turnover_ratio DOUBLE,
    amihud_illiquidity DOUBLE,
    impact_1pct_adv_bps DOUBLE,
    metrics JSON,
    PRIMARY KEY(symbol, as_of)
);

CREATE TABLE IF NOT EXISTS correlation_runs (
    id TEXT PRIMARY KEY,
    target_symbol TEXT,
    as_of DATE,
    lookback_days INTEGER,
    peers JSON,
    metrics JSON
);

CREATE TABLE IF NOT EXISTS etf_premiums (
    symbol TEXT,
    as_of DATE,
    market_price DOUBLE,
    nav DOUBLE,
    premium_pct DOUBLE,
    metrics JSON,
    source TEXT,
    PRIMARY KEY(symbol, as_of, source)
);

CREATE TABLE IF NOT EXISTS analyst_estimates (
    symbol TEXT,
    as_of DATE,
    estimates JSON,
    source TEXT,
    PRIMARY KEY(symbol, as_of, source)
);

CREATE TABLE IF NOT EXISTS earnings_events (
    symbol TEXT,
    event_date DATE,
    event_type TEXT,
    metrics JSON,
    source TEXT,
    PRIMARY KEY(symbol, event_date, event_type, source)
);

CREATE TABLE IF NOT EXISTS earnings_setups (
    symbol TEXT,
    as_of DATE,
    event_date DATE,
    setup_type TEXT,
    score DOUBLE,
    revision_score DOUBLE,
    surprise_score DOUBLE,
    estimate_spread_score DOUBLE,
    sentiment_score DOUBLE,
    verdict TEXT,
    metrics JSON,
    source TEXT,
    PRIMARY KEY(symbol, as_of, setup_type, source)
);

CREATE TABLE IF NOT EXISTS valuation_models (
    symbol TEXT,
    as_of DATE,
    method TEXT,
    fair_value DOUBLE,
    upside_pct DOUBLE,
    assumptions JSON,
    diagnostics JSON,
    PRIMARY KEY(symbol, as_of, method)
);

CREATE TABLE IF NOT EXISTS market_valuation_metric_points (
    metric TEXT,
    as_of DATE,
    label TEXT,
    value DOUBLE,
    suffix TEXT,
    higher_is_better BOOLEAN,
    source TEXT,
    source_url TEXT,
    PRIMARY KEY(metric, as_of, source)
);

CREATE TABLE IF NOT EXISTS market_environment_asset_snapshots (
    symbol TEXT,
    as_of DATE,
    group_name TEXT,
    name TEXT,
    price DOUBLE,
    return_1d DOUBLE,
    return_ytd DOUBLE,
    return_1w DOUBLE,
    return_1m DOUBLE,
    return_1y DOUBLE,
    pct_from_52w_high DOUBLE,
    sma_10_up BOOLEAN,
    sma_20_up BOOLEAN,
    sma_50_up BOOLEAN,
    sma_200_up BOOLEAN,
    sma_20_gt_50 BOOLEAN,
    sma_50_gt_200 BOOLEAN,
    range_ratio_52w DOUBLE,
    color TEXT,
    source TEXT,
    raw JSON,
    PRIMARY KEY(symbol, as_of, group_name, source)
);

CREATE TABLE IF NOT EXISTS preopen_daily_brief (
    brief_date DATE PRIMARY KEY,
    generated_at TIMESTAMP,
    session TEXT,
    status TEXT,
    model_name TEXT,
    model_version TEXT,
    reasoning_effort TEXT,
    headline TEXT,
    macro_regime TEXT,
    narrative TEXT,
    opening_scenario TEXT,
    qqq_path TEXT,
    qqq_forecast JSON,
    key_events JSON,
    watch_items JSON,
    risks JSON,
    context JSON,
    backtest JSON,
    source_models JSON,
    error TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS options_payoff_scenarios (
    id TEXT PRIMARY KEY,
    symbol TEXT,
    as_of TIMESTAMP,
    expiry DATE,
    strategy_type TEXT,
    spot DOUBLE,
    dte INTEGER,
    iv DOUBLE,
    net_premium DOUBLE,
    max_profit DOUBLE,
    max_loss DOUBLE,
    breakevens JSON,
    legs JSON,
    curve JSON,
    diagnostics JSON,
    source TEXT
);

CREATE TABLE IF NOT EXISTS discovered_universe (
    symbol TEXT PRIMARY KEY,
    name TEXT,
    asset_class TEXT,
    inclusion_reasons JSON,
    source_counts JSON,
    latest_source_timestamp TIMESTAMP,
    latest_observed_at TIMESTAMP,
    next_event_at TIMESTAMP,
    eligibility_status TEXT,
    eligibility_detail TEXT,
    evidence_score DOUBLE,
    discovery_score DOUBLE,
    liquidity_score DOUBLE,
    recency_score DOUBLE,
    universe_rank INTEGER,
    decision_universe_member BOOLEAN,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS decision_queue (
    symbol TEXT PRIMARY KEY,
    as_of TIMESTAMP,
    rank INTEGER,
    action_grade TEXT,
    decision_bucket TEXT,
    score DOUBLE,
    discovery_score DOUBLE,
    decision_score DOUBLE,
    action_score DOUBLE,
    freshness_status TEXT,
    quote_freshness TEXT,
    daily_analysis_freshness TEXT,
    filing_freshness TEXT,
    thesis_freshness TEXT,
    overall_decision_freshness TEXT,
    source_cluster TEXT,
    evidence_count INTEGER,
    raw_source_rows INTEGER,
    independent_source_count INTEGER,
    evidence_items_count INTEGER,
    primary_evidence_count INTEGER,
    inclusion_reasons JSON,
    blocking_gates JSON,
    decision_basis JSON,
    latest_quote DOUBLE,
    latest_quote_at TIMESTAMP,
    latest_observed_at TIMESTAMP,
    next_event_at TIMESTAMP,
    catalyst_window TEXT,
    liquidity_grade TEXT,
    portfolio_impact JSON,
    invalidation TEXT
);

CREATE TABLE IF NOT EXISTS source_freshness (
    source_key TEXT PRIMARY KEY,
    source_type TEXT,
    provider TEXT,
    last_observed_at TIMESTAMP,
    freshness_status TEXT,
    stale_after TEXT,
    status TEXT,
    detail TEXT,
    docs_only BOOLEAN,
    checked_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS symbol_decision_snapshots (
    symbol TEXT PRIMARY KEY,
    as_of TIMESTAMP,
    action_grade TEXT,
    freshness_status TEXT,
    quote_freshness TEXT,
    daily_analysis_freshness TEXT,
    filing_freshness TEXT,
    thesis_freshness TEXT,
    source_cluster TEXT,
    inclusion_reasons JSON,
    blocking_gates JSON,
    decision_basis JSON,
    snapshot JSON
);

CREATE TABLE IF NOT EXISTS refresh_jobs (
    id TEXT PRIMARY KEY,
    job_name TEXT,
    status TEXT,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    error TEXT,
    summary JSON
);

CREATE TABLE IF NOT EXISTS broker_provider_status (
    provider TEXT PRIMARY KEY,
    checked_at TIMESTAMP,
    status TEXT,
    health TEXT,
    detail TEXT,
    account_id TEXT,
    account_mode TEXT,
    session_started_at TIMESTAMP,
    last_data_at TIMESTAMP,
    latency_ms DOUBLE,
    capabilities JSON,
    raw JSON
);

CREATE TABLE IF NOT EXISTS broker_accounts (
    provider TEXT,
    account_id TEXT,
    account_mode TEXT,
    currency TEXT,
    cash DOUBLE,
    buying_power DOUBLE,
    net_liquidation DOUBLE,
    margin_requirement DOUBLE,
    excess_liquidity DOUBLE,
    day_pnl DOUBLE,
    total_pnl DOUBLE,
    updated_at TIMESTAMP,
    raw JSON,
    PRIMARY KEY(provider, account_id)
);

CREATE TABLE IF NOT EXISTS broker_positions (
    provider TEXT,
    account_id TEXT,
    symbol TEXT,
    asset_class TEXT,
    quantity DOUBLE,
    average_cost DOUBLE,
    market_price DOUBLE,
    market_value DOUBLE,
    unrealized_pnl DOUBLE,
    realized_pnl DOUBLE,
    updated_at TIMESTAMP,
    raw JSON,
    PRIMARY KEY(provider, account_id, symbol)
);

CREATE TABLE IF NOT EXISTS broker_orders (
    provider TEXT,
    account_id TEXT,
    order_id TEXT,
    symbol TEXT,
    side TEXT,
    order_type TEXT,
    quantity DOUBLE,
    limit_price DOUBLE,
    status TEXT,
    submitted_at TIMESTAMP,
    updated_at TIMESTAMP,
    raw JSON,
    PRIMARY KEY(provider, account_id, order_id)
);

CREATE TABLE IF NOT EXISTS broker_fills (
    provider TEXT,
    account_id TEXT,
    fill_id TEXT,
    order_id TEXT,
    symbol TEXT,
    side TEXT,
    quantity DOUBLE,
    price DOUBLE,
    filled_at TIMESTAMP,
    raw JSON,
    PRIMARY KEY(provider, account_id, fill_id)
);

CREATE TABLE IF NOT EXISTS broker_market_snapshots (
    provider TEXT,
    symbol TEXT,
    observed_at TIMESTAMP,
    bid DOUBLE,
    ask DOUBLE,
    last DOUBLE,
    close DOUBLE,
    volume DOUBLE,
    entitlement_status TEXT,
    data_status TEXT,
    raw JSON,
    PRIMARY KEY(provider, symbol, observed_at)
);

CREATE TABLE IF NOT EXISTS broker_scanner_signals (
    provider TEXT,
    run_id TEXT,
    symbol TEXT,
    observed_at TIMESTAMP,
    signal_type TEXT,
    rank INTEGER,
    score DOUBLE,
    metrics JSON,
    raw JSON,
    PRIMARY KEY(provider, run_id, symbol, signal_type)
);

CREATE TABLE IF NOT EXISTS broker_agent_recommendations (
    id TEXT PRIMARY KEY,
    symbol TEXT,
    as_of TIMESTAMP,
    action TEXT,
    status TEXT,
    actionability_score DOUBLE,
    thesis TEXT,
    setup_type TEXT,
    entry_trigger TEXT,
    invalidation_stop TEXT,
    target TEXT,
    risk_reward TEXT,
    sizing JSON,
    max_notional DOUBLE,
    portfolio_impact JSON,
    evidence JSON,
    blockers JSON,
    data_freshness JSON,
    paper_order_preview JSON,
    policy_checks JSON,
    authority TEXT
);

CREATE TABLE IF NOT EXISTS broker_policy_checks (
    id TEXT PRIMARY KEY,
    recommendation_id TEXT,
    symbol TEXT,
    checked_at TIMESTAMP,
    check_name TEXT,
    status TEXT,
    detail TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS broker_paper_orders (
    id TEXT PRIMARY KEY,
    recommendation_id TEXT,
    provider TEXT,
    account_id TEXT,
    symbol TEXT,
    side TEXT,
    order_type TEXT,
    quantity DOUBLE,
    limit_price DOUBLE,
    notional DOUBLE,
    status TEXT,
    authority TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    preview JSON,
    audit_trail JSON
);
"""
