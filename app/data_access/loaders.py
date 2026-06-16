"""Panel read-model loading and scope selection."""

from __future__ import annotations
from datetime import date, datetime
from typing import Any, Iterable
from app.panel_contracts import (
    panel_contract_payload as contract_panel_payload,
)
from investment_panel.core.panel import (
    load_panel_data as core_load_panel_data,
    load_ticker_dossier_data as core_load_ticker_dossier_data,
)

from app.data_access.types import DataStatus, PanelData, SETUP_INSTRUCTIONS
from app.data_access.config import _database_path, load_config, tables_for_scope
from app.data_access.coerce import _is_empty
from app.data_access.normalize import _normalize_panel_data
from app.data_access.payloads import _runtime_metadata



def load_panel_data(
    config: dict[str, Any] | None = None,
    table_names: Iterable[str] | None = None,
    ensure_decision_models: bool | None = None,
    ensure_source_models: bool | None = None,
) -> PanelData:
    """Load panel read models from core and normalize them for the API."""

    active_config = config or load_config()
    requested_table_names = None if table_names is None else tuple(table_names)
    try:
        raw_data = core_load_panel_data(
            active_config,
            table_names=requested_table_names,
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
    if _is_empty(panel_data) and panel_data.status.source != "duckdb-missing" and requested_table_names != ():
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
        ensure_decision_models=False,
        ensure_source_models=False,
    )




def load_table_panel_data(config: dict[str, Any] | None, table_name: str) -> PanelData:
    """Load the minimum backend read model for one table endpoint."""

    return load_panel_data(
        config,
        table_names=(table_name,),
        ensure_decision_models=False,
        ensure_source_models=False,
    )




def load_ticker_panel_data(config: dict[str, Any] | None, ticker: str) -> PanelData:
    """Load only ticker dossier read models before symbol filtering."""

    normalized = ticker.strip().upper()
    if not normalized:
        return PanelData(status=DataStatus(False, "Ticker is required.", "invalid-request"), tables={})
    try:
        raw_data = core_load_ticker_dossier_data(config or load_config(), normalized)
        return _normalize_panel_data(raw_data)
    except Exception as exc:
        return PanelData(
            status=DataStatus(False, f"Ticker dossier helper failed: {exc}", "core-error"),
            tables={},
            metadata={"setup_instructions": SETUP_INSTRUCTIONS},
        )




def panel_contract_payload() -> dict[str, Any]:
    return contract_panel_payload()




def load_market_panel_data(config: dict[str, Any] | None = None) -> PanelData:
    """Load only the broad-market tables required by the Market page."""

    active_config = config or load_config()
    from investment_panel.core.panel.read_session import panel_read_session
    from investment_panel.core.panel import market_environment_assets, market_environment_model, market_valuation_reference_charts

    db_path = _database_path(active_config)
    try:
        with panel_read_session(db_path, needs_write=False) as con:
            if con is None:
                return _empty_market_panel_data(
                    "DuckDB database does not exist yet. Run a refresh job to initialize it.",
                    "duckdb-missing",
                )
            tables = {
                "market_valuation_reference_charts": market_valuation_reference_charts(con),
                "market_environment_assets": market_environment_assets(con),
                "market_environment_model": market_environment_model(con, [], include_exposure=False),
            }
    except Exception as exc:
        return _empty_market_panel_data(f"Market read models are unavailable: {exc}", "core-error")
    ready = any(tables.values())
    freshness = _market_freshness(tables)
    status_source = "duckdb-stale" if freshness.get("status") == "stale" else "duckdb"
    message = "Loaded market environment data."
    if freshness.get("status") == "stale":
        message = f"Loaded market environment data, but broad-market inputs are stale: {freshness.get('reason')}"
    return PanelData(
        status=DataStatus(
            ready=ready,
            message=message if ready else "No market environment rows are loaded yet.",
            source=status_source,
        ),
        tables=tables,
        metadata={"market_freshness": freshness},
    )


def _empty_market_panel_data(message: str, source: str) -> PanelData:
    return PanelData(
        status=DataStatus(ready=False, message=message, source=source),
        tables={
            "market_valuation_reference_charts": [],
            "market_environment_assets": [],
            "market_environment_model": [],
        },
        metadata={"setup_instructions": SETUP_INSTRUCTIONS},
    )


def _market_freshness(tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    today = date.today()
    reference_date = _max_row_date(tables.get("market_valuation_reference_charts") or [], ("latest_date", "as_of", "date"))
    asset_date = _max_row_date(tables.get("market_environment_assets") or [], ("as_of", "date"))
    checks = {
        "valuation_reference": _freshness_check(reference_date, today, max_age_days=1),
        "asset_matrix": _freshness_check(asset_date, today, max_age_days=1),
    }
    stale = {name: check for name, check in checks.items() if check["status"] == "stale"}
    unknown = {name: check for name, check in checks.items() if check["status"] == "unknown"}
    if stale:
        status = "stale"
        reason = "; ".join(f"{name} latest {check['latest_date']} is {check['age_days']}d old" for name, check in stale.items())
    elif unknown:
        status = "unknown"
        reason = "Market freshness dates are not loaded."
    else:
        status = "fresh"
        reason = "Broad-market inputs are within freshness windows."
    return {"status": status, "as_of": today.isoformat(), "reason": reason, "checks": checks}


def _freshness_check(value: date | None, today: date, *, max_age_days: int) -> dict[str, Any]:
    if value is None:
        return {"status": "unknown", "latest_date": None, "age_days": None, "max_age_days": max_age_days}
    age_days = (today - value).days
    return {
        "status": "stale" if age_days > max_age_days else "fresh",
        "latest_date": value.isoformat(),
        "age_days": age_days,
        "max_age_days": max_age_days,
    }


def _max_row_date(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> date | None:
    dates = [_date_value(row.get(key)) for row in rows for key in keys]
    valid_dates = [value for value in dates if value is not None]
    return max(valid_dates) if valid_dates else None


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None
