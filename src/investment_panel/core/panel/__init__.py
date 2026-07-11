"""Panel contracts with lazy access to retired DuckDB read models.

The PostgreSQL app imports only the contract names below. Legacy tests may ask
for an older read-model function; those modules are loaded on demand so they do
not pull DuckDB into the live API process.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from investment_panel.core.panel.contracts import (
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

_LEGACY_MODULES = (
    "catalog", "coerce", "sources", "technicals", "disclosures",
    "market_environment", "market_freshness", "feed", "read_equity",
    "read_market_data", "read_research", "read_tradingview", "read_options",
    "read_learning", "payloads", "read_session", "registry", "snapshot",
    "ticker_dossier", "ticker_sections",
)

_CALLABLES = {
    "build_source_catalog_health": "catalog",
    "build_ticker_dossier": "ticker_sections",
    "dashboard_payload": "payloads",
    "disclosures": "disclosures",
    "feed_signals": "feed",
    "liquidity": "read_market_data",
    "load_panel_data": "snapshot",
    "load_ticker_dossier_data": "snapshot",
    "market_environment_assets": "market_environment",
    "market_environment_model": "market_environment",
    "market_freshness": "market_freshness",
    "market_valuation_charts": "market_environment",
    "market_valuation_reference_charts": "market_environment",
    "ownership_consensus": "feed",
    "panel_snapshot_payload": "payloads",
    "quotes": "read_market_data",
    "read_model_names": "registry",
    "screener": "read_market_data",
    "sepa": "read_market_data",
    "source_consensus": "feed",
    "source_ticker_ranking_rows": "sources",
    "technicals": "technicals",
    "universe_screen": "feed",
    "valuations": "read_market_data",
}


def _lazy_callable(name: str, module_name: str):
    def call(*args: Any, **kwargs: Any) -> Any:
        function = getattr(import_module(f"investment_panel.core.panel.{module_name}"), name)
        return function(*args, **kwargs)

    call.__name__ = name
    return call


for _name, _module_name in _CALLABLES.items():
    globals()[_name] = _lazy_callable(_name, _module_name)


def __getattr__(name: str) -> Any:
    for module_name in _LEGACY_MODULES:
        module = import_module(f"investment_panel.core.panel.{module_name}")
        if hasattr(module, name):
            value = getattr(module, name)
            globals()[name] = value
            return value
    raise AttributeError(name)


__all__ = [
    "DECISION_REPAIR_TABLES", "ENDPOINT_TABLES", "FRONTEND_TABLE_KEY_OVERRIDES",
    "PANEL_SCOPE_TABLES", "SOURCE_REPAIR_TABLES", "TICKER_TABLES",
    "WATCHLIST_SECTION_OUTPUT_TABLES", "WATCHLIST_SECTION_TABLES",
    "frontend_key_for_table", "panel_contract_payload", "panel_snapshot_table_names",
    "table_for_endpoint", "tables_for_scope",
    "build_source_catalog_health", "build_ticker_dossier", "dashboard_payload",
    "disclosures", "feed_signals", "liquidity", "load_panel_data",
    "load_ticker_dossier_data", "market_environment_assets", "market_environment_model",
    "market_freshness", "market_valuation_charts", "market_valuation_reference_charts",
    "ownership_consensus", "panel_snapshot_payload", "quotes", "read_model_names",
    "screener", "sepa", "source_consensus", "source_ticker_ranking_rows",
    "technicals", "universe_screen", "valuations",
]
