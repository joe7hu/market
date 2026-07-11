"""PostgreSQL panel read-model loading and scope selection."""

from __future__ import annotations

from typing import Any, Iterable

from app.data_access.config import load_config, tables_for_scope
from app.data_access.postgres_panel import load_postgres_tables
from app.data_access.types import DataStatus, PanelData
from app.panel_contracts import panel_contract_payload as contract_panel_payload


def load_panel_data(
    config: dict[str, Any] | None = None,
    table_names: Iterable[str] | None = None,
    ensure_decision_models: bool | None = None,
    ensure_source_models: bool | None = None,
) -> PanelData:
    del ensure_decision_models, ensure_source_models
    active_config = config or load_config()
    requested = _all_contract_tables() if table_names is None else tuple(table_names)
    if not requested:
        return PanelData(
            status=DataStatus(True, "No PostgreSQL read models requested.", "postgresql"),
            tables={},
            metadata={"database": "postgresql", "table_count": 0},
        )
    try:
        tables, metadata = load_postgres_tables(active_config, requested)
    except Exception as exc:
        return PanelData(
            status=DataStatus(False, f"PostgreSQL read models unavailable: {exc}", "postgresql-error"),
            tables={name: [] for name in requested},
            metadata={"database": "postgresql", "error": str(exc)},
        )
    unavailable = list(metadata.get("unavailable_models") or [])
    available_count = int(metadata.get("available_model_count") or 0)
    if unavailable:
        message = f"PostgreSQL loaded with {len(unavailable)} explicitly unavailable read models."
        status = DataStatus(available_count > 0, message, "postgresql-partial")
    else:
        status = DataStatus(True, "PostgreSQL read models loaded.", "postgresql")
    return PanelData(
        status=status,
        tables=tables,
        metadata=metadata,
    )


def load_panel_scope_data(config: dict[str, Any] | None, scope: str) -> PanelData:
    return load_panel_data(config, table_names=tables_for_scope(scope))


def load_table_panel_data(config: dict[str, Any] | None, table_name: str) -> PanelData:
    return load_panel_data(config, table_names=(table_name,))


def load_ticker_panel_data(config: dict[str, Any] | None, ticker: str) -> PanelData:
    normalized = ticker.strip().upper()
    if not normalized:
        return PanelData(status=DataStatus(False, "Ticker is required.", "invalid-request"), tables={})
    panel = load_panel_data(config)
    panel.tables = {
        name: [row for row in rows if _row_symbol(row) in {"", normalized}]
        for name, rows in panel.tables.items()
    }
    panel.metadata["ticker"] = normalized
    return panel


def panel_contract_payload() -> dict[str, Any]:
    return contract_panel_payload()


def load_market_panel_data(config: dict[str, Any] | None = None) -> PanelData:
    return load_panel_scope_data(config, "market")


def _all_contract_tables() -> tuple[str, ...]:
    contract = contract_panel_payload()
    values = set(contract.get("tables") or [])
    for names in (contract.get("scopes") or {}).values():
        values.update(names or [])
    values.update(contract.get("ticker_tables") or [])
    return tuple(sorted(values))


def _row_symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("ticker") or "").upper()
