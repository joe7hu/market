"""Panel read-model loading and scope selection."""

from __future__ import annotations
from typing import Any, Iterable
from app.panel_contracts import (
    DECISION_REPAIR_TABLES,
    SOURCE_REPAIR_TABLES,
    TICKER_TABLES,
    panel_contract_payload as contract_panel_payload,
    tables_for_scope as contract_tables_for_scope,
)
from investment_panel.core.panel import (
    load_panel_data as core_load_panel_data,
    load_ticker_dossier_data as core_load_ticker_dossier_data,
)

from app.data_access.types import DataStatus, PanelData, SETUP_INSTRUCTIONS
from app.data_access.config import _database_path, load_config, tables_for_scope
from app.data_access.coerce import _is_empty
from app.data_access.normalize import _normalize_panel_data
from app.data_access.payloads import _filter_ticker_panel_data, _runtime_metadata



def load_panel_data(
    config: dict[str, Any] | None = None,
    table_names: Iterable[str] | None = None,
    ensure_decision_models: bool | None = None,
    ensure_source_models: bool | None = None,
) -> PanelData:
    """Load panel read models from core and normalize them for the API."""

    active_config = config or load_config()
    try:
        raw_data = core_load_panel_data(
            active_config,
            table_names=tuple(table_names or ()),
            ensure_decision_models=ensure_decision_models,
            ensure_source_models=ensure_source_models,
        )
    except Exception as exc:  # pragma: no cover - defensive UI boundary
        return PanelData(
            status=DataStatus(
                ready=False,
                message=f"Core data helper failed: {exc}",
                source="core-error",
            ),
            metadata={"setup_instructions": SETUP_INSTRUCTIONS},
        )

    panel_data = _normalize_panel_data(raw_data)
    panel_data.metadata.update(_runtime_metadata(active_config))
    if _is_empty(panel_data):
        panel_data.status = DataStatus(
            ready=False,
            message="Core helpers returned no rows for the configured DuckDB.",
            source="empty-db",
        )
        panel_data.metadata.setdefault("setup_instructions", SETUP_INSTRUCTIONS)
    return panel_data




def load_panel_scope_data(config: dict[str, Any] | None, scope: str) -> PanelData:
    """Load the minimum backend read models needed for one app scope."""

    scope_tables = tables_for_scope(scope)
    return load_panel_data(
        config,
        table_names=scope_tables,
        ensure_decision_models=bool(set(scope_tables) & DECISION_REPAIR_TABLES),
        ensure_source_models=bool(set(scope_tables) & SOURCE_REPAIR_TABLES),
    )




def load_table_panel_data(config: dict[str, Any] | None, table_name: str) -> PanelData:
    """Load the minimum backend read model for one table endpoint."""

    return load_panel_data(
        config,
        table_names=(table_name,),
        ensure_decision_models=table_name in DECISION_REPAIR_TABLES,
        ensure_source_models=table_name in SOURCE_REPAIR_TABLES,
    )




def load_ticker_panel_data(config: dict[str, Any] | None, ticker: str) -> PanelData:
    """Load only ticker dossier read models before symbol filtering."""

    normalized = ticker.strip().upper()
    if not normalized:
        return PanelData(status=DataStatus(False, "Ticker is required.", "invalid-request"), tables={})
    try:
        raw_data = core_load_ticker_dossier_data(config or load_config(), normalized)
        return _normalize_panel_data(raw_data)
    except Exception:
        return _filter_ticker_panel_data(load_panel_data(config, table_names=TICKER_TABLES), normalized)




def panel_contract_payload() -> dict[str, Any]:
    return contract_panel_payload()




def load_market_panel_data(config: dict[str, Any] | None = None) -> PanelData:
    """Load only the broad-market tables required by the Market page."""

    active_config = config or load_config()
    from investment_panel.core.db import db, init_db
    from investment_panel.core.panel import market_environment_assets, market_environment_model, market_valuation_reference_charts

    db_path = _database_path(active_config)
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        tables = {
            "market_valuation_reference_charts": market_valuation_reference_charts(con),
            "market_environment_assets": market_environment_assets(con),
            "market_environment_model": market_environment_model(con, [], include_exposure=False),
        }
    ready = any(tables.values())
    return PanelData(
        status=DataStatus(
            ready=ready,
            message="Loaded market environment data." if ready else "No market environment rows are loaded yet.",
            source="duckdb",
        ),
        tables=tables,
        metadata={},
    )
