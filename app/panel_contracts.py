"""Compatibility facade for panel route/table contracts.

The canonical contract catalog is exported from :mod:`investment_panel.core.panel`
so read-model dispatch, API payloads, and tests share one backend-owned module.
"""

from investment_panel.core.panel import (
    DECISION_REPAIR_TABLES,
    ENDPOINT_TABLES,
    FRONTEND_TABLE_KEY_OVERRIDES,
    PANEL_SCOPE_TABLES,
    SOURCE_REPAIR_TABLES,
    TICKER_TABLES,
    WATCHLIST_SECTION_OUTPUT_TABLES,
    WATCHLIST_SECTION_TABLES,
    frontend_key_for_table,
    panel_contract_payload,
    panel_snapshot_table_names,
    table_for_endpoint,
    tables_for_scope,
)

__all__ = [
    "DECISION_REPAIR_TABLES",
    "ENDPOINT_TABLES",
    "FRONTEND_TABLE_KEY_OVERRIDES",
    "PANEL_SCOPE_TABLES",
    "SOURCE_REPAIR_TABLES",
    "TICKER_TABLES",
    "WATCHLIST_SECTION_OUTPUT_TABLES",
    "WATCHLIST_SECTION_TABLES",
    "frontend_key_for_table",
    "panel_contract_payload",
    "panel_snapshot_table_names",
    "table_for_endpoint",
    "tables_for_scope",
]
