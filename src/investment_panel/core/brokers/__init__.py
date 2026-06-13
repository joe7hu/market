"""Broker integration: providers, sync, persistence, and read models."""

from __future__ import annotations

from investment_panel.core.brokers.constants import ADVISORY_AUTHORITY, BROKER_BLOCKING_STATUSES, IBKR_ACCOUNT_TAGS, IBKR_GENERIC_TICKS, IBKR_TICK_GENERIC_FIELDS, IBKR_TICK_PRICE_FIELDS, IBKR_TICK_SIZE_FIELDS
from investment_panel.core.brokers.types import BrokerProvider, BrokerSnapshot, ProviderStatus
from investment_panel.core.brokers.coerce import parse_dt, parse_json, stable_id, tcp_open
from investment_panel.core.brokers.ibkr import IBKRProvider, collect_ibkr_snapshot, ibkr_accept_account, ibkr_account_mode_mismatch, ibkr_accounts, ibkr_asset_class, ibkr_capabilities, ibkr_contract_raw, ibkr_entitlement_errors, ibkr_execution_time, ibkr_health, ibkr_market_data_status, ibkr_market_data_type_id, ibkr_market_snapshots, ibkr_missing_quote_symbols, ibkr_number, ibkr_object_raw, ibkr_paper_account_id, ibkr_paper_account_mismatch, ibkr_position_symbol, ibkr_positions, ibkr_quote_symbols, ibkr_session_failure, ibkr_snapshot_status, ibkr_stock_quote_symbol
from investment_panel.core.brokers.moomoo import MoomooProvider, moomoo_capabilities
from investment_panel.core.brokers.persistence import clear_broker_account_read_models, persist_broker_quote_rows, persist_broker_snapshot, record_provider_run, record_source_health
from investment_panel.core.brokers.read_models import agent_recommendations, broker_accounts, broker_market_snapshots, broker_positions, broker_scanner_signals, broker_status_rows, decode_broker_row, effective_portfolio_rows, paper_orders
from investment_panel.core.brokers.policy import manual_account_proxy, policy_checks
from investment_panel.core.brokers.recommendations import build_agent_recommendations, build_and_persist_agent_recommendations, entry_trigger_for, recommendation_evidence, recommendation_portfolio_impact, risk_reward_for, setup_type_for, stage_paper_order, target_for
from investment_panel.core.brokers.service import broker_sync_symbols, run, update_broker_sources

__all__ = [
    "ADVISORY_AUTHORITY",
    "BROKER_BLOCKING_STATUSES",
    "BrokerProvider",
    "BrokerSnapshot",
    "IBKRProvider",
    "IBKR_ACCOUNT_TAGS",
    "IBKR_GENERIC_TICKS",
    "IBKR_TICK_GENERIC_FIELDS",
    "IBKR_TICK_PRICE_FIELDS",
    "IBKR_TICK_SIZE_FIELDS",
    "MoomooProvider",
    "ProviderStatus",
    "agent_recommendations",
    "broker_accounts",
    "broker_market_snapshots",
    "broker_positions",
    "broker_scanner_signals",
    "broker_status_rows",
    "broker_sync_symbols",
    "build_agent_recommendations",
    "build_and_persist_agent_recommendations",
    "clear_broker_account_read_models",
    "collect_ibkr_snapshot",
    "decode_broker_row",
    "effective_portfolio_rows",
    "entry_trigger_for",
    "ibkr_accept_account",
    "ibkr_account_mode_mismatch",
    "ibkr_accounts",
    "ibkr_asset_class",
    "ibkr_capabilities",
    "ibkr_contract_raw",
    "ibkr_entitlement_errors",
    "ibkr_execution_time",
    "ibkr_health",
    "ibkr_market_data_status",
    "ibkr_market_data_type_id",
    "ibkr_market_snapshots",
    "ibkr_missing_quote_symbols",
    "ibkr_number",
    "ibkr_object_raw",
    "ibkr_paper_account_id",
    "ibkr_paper_account_mismatch",
    "ibkr_position_symbol",
    "ibkr_positions",
    "ibkr_quote_symbols",
    "ibkr_session_failure",
    "ibkr_snapshot_status",
    "ibkr_stock_quote_symbol",
    "manual_account_proxy",
    "moomoo_capabilities",
    "paper_orders",
    "parse_dt",
    "parse_json",
    "persist_broker_quote_rows",
    "persist_broker_snapshot",
    "policy_checks",
    "recommendation_evidence",
    "recommendation_portfolio_impact",
    "record_provider_run",
    "record_source_health",
    "risk_reward_for",
    "run",
    "setup_type_for",
    "stable_id",
    "stage_paper_order",
    "target_for",
    "tcp_open",
    "update_broker_sources",
]
