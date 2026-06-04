"""DuckDB schema and repository helpers."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import duckdb


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
    validated_at TIMESTAMP,
    state TEXT,
    reason TEXT,
    option_still_valid BOOLEAN,
    stock_progress TEXT,
    iv_status TEXT,
    candidate_state TEXT,
    evidence_refs JSON,
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


def connect(path: str | Path, read_only: bool = False, retries: int = 30, delay_seconds: float = 1.0) -> duckdb.DuckDBPyConnection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return duckdb.connect(str(db_path), read_only=read_only)
        except duckdb.IOException as exc:
            if "Could not set lock on file" not in str(exc) or attempt >= retries:
                raise
            last_error = exc
            time.sleep(delay_seconds)
    raise last_error or RuntimeError(f"Could not connect to DuckDB: {db_path}")


def init_db(path: str | Path) -> None:
    with connect(path) as con:
        con.sql(SCHEMA_SQL)
        _migrate_schema(con)


def _migrate_schema(con: duckdb.DuckDBPyConnection) -> None:
    columns = {row[1] for row in con.execute("PRAGMA table_info('portfolio_positions')").fetchall()}
    if "purchase_date" not in columns:
        con.execute("ALTER TABLE portfolio_positions ADD COLUMN purchase_date DATE")
    catalyst_columns = {row[1] for row in con.execute("PRAGMA table_info('catalysts')").fetchall()}
    for column, column_type in {
        "start_at": "TIMESTAMP",
        "end_at": "TIMESTAMP",
        "timezone": "TEXT",
        "event_scope": "TEXT",
        "event_kind": "TEXT",
        "importance": "TEXT",
        "verification_status": "TEXT",
        "source_url": "TEXT",
        "source_name": "TEXT",
    }.items():
        if column not in catalyst_columns:
            con.execute(f"ALTER TABLE catalysts ADD COLUMN {column} {column_type}")
    for table, columns_to_add in {
        "discovered_universe": {
            "latest_observed_at": "TIMESTAMP",
            "next_event_at": "TIMESTAMP",
            "discovery_score": "DOUBLE",
        },
        "decision_queue": {
            "discovery_score": "DOUBLE",
            "decision_score": "DOUBLE",
            "action_score": "DOUBLE",
            "quote_freshness": "TEXT",
            "daily_analysis_freshness": "TEXT",
            "filing_freshness": "TEXT",
            "thesis_freshness": "TEXT",
            "overall_decision_freshness": "TEXT",
            "raw_source_rows": "INTEGER",
            "independent_source_count": "INTEGER",
            "evidence_items_count": "INTEGER",
            "primary_evidence_count": "INTEGER",
            "latest_observed_at": "TIMESTAMP",
            "next_event_at": "TIMESTAMP",
        },
        "symbol_decision_snapshots": {
            "quote_freshness": "TEXT",
            "daily_analysis_freshness": "TEXT",
            "filing_freshness": "TEXT",
            "thesis_freshness": "TEXT",
        },
        "source_registry": {
            "source_family": "TEXT",
            "raw_access": "TEXT",
        },
        "source_runs": {
            "item_count": "INTEGER",
            "ticker_count": "INTEGER",
            "failure_detail": "TEXT",
        },
        "source_items": {
            "source_run_id": "TEXT",
            "content_hash": "TEXT",
            "license_status": "TEXT",
        },
        "ticker_source_signals": {
            "needs_market_context": "BOOLEAN",
        },
        "manual_watchlist": {
            "watch_state": "TEXT",
        },
        "options_chain": {
            "rho": "DOUBLE",
            "theo": "DOUBLE",
            "bid_iv": "DOUBLE",
            "ask_iv": "DOUBLE",
            "contract_symbol": "TEXT",
        },
    }.items():
        existing_columns = {row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
        for column, column_type in columns_to_add.items():
            if column not in existing_columns:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
    con.execute("UPDATE manual_watchlist SET watch_state = 'watched' WHERE watch_state IS NULL OR watch_state = ''")


@contextmanager
def db(path: str | Path, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    con = connect(path, read_only=read_only)
    try:
        yield con
    finally:
        con.close()


def json_dumps(value: Any) -> str:
    def default(item: Any) -> Any:
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        return str(item)

    return json.dumps(value, ensure_ascii=False, default=default)


def upsert_instrument(con: duckdb.DuckDBPyConnection, instrument: dict[str, Any]) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO instruments
        (symbol, name, asset_class, sector, industry, category, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            instrument["symbol"].upper(),
            instrument.get("name"),
            instrument.get("asset_class"),
            instrument.get("sector"),
            instrument.get("industry"),
            instrument.get("category"),
            instrument.get("source"),
        ],
    )


def query_rows(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    result = con.execute(sql, params or [])
    columns = [column[0] for column in result.description]
    return [dict(zip(columns, row, strict=False)) for row in result.fetchall()]
