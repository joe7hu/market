"""Data loading and JSON normalization for the investment panel API."""

from __future__ import annotations

from app.data_access.payloads import _runtime_metadata

from app.data_access.types import DataStatus, PanelData, SETUP_INSTRUCTIONS
from app.data_access.config import database_path, database_url, load_config, project_root, tables_for_scope
from app.data_access.coerce import jsonable, normalize_rows
from app.data_access.mutations import delete_portfolio_position, delete_watchlist_symbol, mark_thesis_reviewed, populate_watchlist_symbol_data, save_portfolio_position, save_thesis, save_watchlist_symbol
from app.data_access.loaders import load_market_panel_data, load_panel_data, load_panel_scope_data, load_table_panel_data, load_ticker_panel_data, panel_contract_payload
from app.data_access.payloads import dashboard_payload, panel_snapshot_payload, signals_payload, status_payload, table_payload, ticker_payload, watchlist_section_payload
from app.data_access.decision_brief import GATE_LABELS, ticker_decision_brief
from app.data_access.settings import agent_control_payload, settings_payload, update_agent_settings_config, update_research_sources_config
from app.data_access.user_state import portfolio_rows, table_payload as user_state_table_payload, thesis_monitor_rows, thesis_rows, watchlist_rows

__all__ = [
    "DataStatus",
    "GATE_LABELS",
    "PanelData",
    "SETUP_INSTRUCTIONS",
    "_runtime_metadata",
    "agent_control_payload",
    "dashboard_payload",
    "database_path",
    "database_url",
    "delete_portfolio_position",
    "delete_watchlist_symbol",
    "jsonable",
    "load_config",
    "load_market_panel_data",
    "load_panel_data",
    "load_panel_scope_data",
    "load_table_panel_data",
    "load_ticker_panel_data",
    "mark_thesis_reviewed",
    "normalize_rows",
    "panel_contract_payload",
    "panel_snapshot_payload",
    "portfolio_rows",
    "populate_watchlist_symbol_data",
    "project_root",
    "save_portfolio_position",
    "save_thesis",
    "save_watchlist_symbol",
    "settings_payload",
    "signals_payload",
    "status_payload",
    "table_payload",
    "tables_for_scope",
    "ticker_decision_brief",
    "ticker_payload",
    "thesis_monitor_rows",
    "thesis_rows",
    "update_agent_settings_config",
    "update_research_sources_config",
    "watchlist_section_payload",
    "watchlist_rows",
    "user_state_table_payload",
]
