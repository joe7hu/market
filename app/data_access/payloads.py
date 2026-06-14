"""API payload builders for panel views."""

from __future__ import annotations
import os
from typing import Any
from app.panel_contracts import panel_contract_payload as contract_panel_payload
from investment_panel.core.panel.payloads import (
    dashboard_payload as core_dashboard_payload,
    panel_snapshot_payload as core_panel_snapshot_payload,
)
from investment_panel.core.panel.ticker_dossier import ticker_payload_tables
from investment_panel.core.option_agent_thesis import DEFAULT_AGENT_THESIS_REQUEST_LIMIT

from app.data_access.types import PanelData
from app.data_access.coerce import _int_value, jsonable
from app.data_access.ticker_dossier import _ensure_ticker_dossier_tables
from app.data_access.decision_brief import ticker_decision_brief



def status_payload(panel_data: PanelData) -> dict[str, Any]:
    return {
        "ready": panel_data.status.ready,
        "message": panel_data.status.message,
        "source": panel_data.status.source,
        "metadata": jsonable(panel_data.metadata),
    }




def _runtime_metadata(config: dict[str, Any]) -> dict[str, Any]:
    agents = config.get("agents", {}) if isinstance(config.get("agents"), dict) else {}
    option_thesis = agents.get("option_thesis", {}) if isinstance(agents.get("option_thesis"), dict) else {}
    option_postmortem = agents.get("option_postmortem", {}) if isinstance(agents.get("option_postmortem"), dict) else {}
    return {
        "agents": {
            "option_thesis": _agent_runtime_metadata(option_thesis, default_limit=20) | {
                "request_cap": DEFAULT_AGENT_THESIS_REQUEST_LIMIT,
                "queue_policy": "current_top_ranked_candidates_only",
                "cadence": "daily_premarket",
                "max_runs_per_day": 1,
            },
            "option_postmortem": _agent_runtime_metadata(option_postmortem, default_limit=20) | {
                "cadence": "daily_premarket",
                "max_runs_per_day": 1,
            },
        },
        "options_radar": {
            "deterministic_cadence": "hourly",
            "agent_cadence": "daily_premarket",
        },
        "scheduler": {
            "agent_refresh_seconds": os.environ.get("MARKET_AGENT_REFRESH_SECONDS", "0"),
            "radar_refresh_seconds": os.environ.get("MARKET_RADAR_REFRESH_SECONDS", "900"),
            "source_refresh_seconds": os.environ.get("MARKET_SOURCE_REFRESH_SECONDS", "3600"),
            "learning_refresh_seconds": os.environ.get("MARKET_LEARNING_REFRESH_SECONDS", "21600"),
            "radar_option_source": os.environ.get("MARKET_RADAR_OPTION_SOURCE", "robinhood"),
        },
    }




def _agent_runtime_metadata(config: dict[str, Any], *, default_limit: int) -> dict[str, Any]:
    command = str(config.get("command") or "")
    enabled = bool(config.get("enabled", bool(command)))
    configured = bool(command.strip())
    return {
        "enabled": enabled,
        "configured": configured,
        "active": enabled and configured,
        "status": "active" if enabled and configured else "paused",
        "limit": _int_value(config.get("limit"), default_limit),
        "timeout_seconds": _int_value(config.get("timeout_seconds"), 120),
    }




def table_payload(panel_data: PanelData, table_name: str) -> dict[str, Any]:
    rows = panel_data.rows(table_name)
    return {"rows": rows, "count": len(rows), "status": status_payload(panel_data)}




def signals_payload(panel_data: PanelData) -> dict[str, Any]:
    rows = panel_data.rows("signals") or panel_data.rows("candidates")
    return {"rows": rows, "count": len(rows), "status": status_payload(panel_data)}




def dashboard_payload(panel_data: PanelData) -> dict[str, Any]:
    return core_dashboard_payload(status_payload(panel_data), panel_data.rows)




def panel_snapshot_payload(panel_data: PanelData, scope: str, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
    return core_panel_snapshot_payload(
        scope=scope,
        status=status_payload(panel_data),
        rows_for_table=panel_data.rows,
        offset=offset,
        limit=limit,
    )


def watchlist_section_payload(panel_data: PanelData, scope: str, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
    return panel_snapshot_payload(panel_data, scope, offset=offset, limit=limit)




def ticker_payload(panel_data: PanelData, ticker: str) -> dict[str, Any]:
    normalized_ticker = ticker.upper()
    tables = ticker_payload_tables(panel_data.rows, normalized_ticker)
    _ensure_ticker_dossier_tables(normalized_ticker, tables)
    return {
        "ticker": normalized_ticker,
        "status": status_payload(panel_data),
        "tables": tables,
        "decision_snapshot": (tables["symbol_decision_snapshot"] or tables["symbol_decision_snapshots"] or [None])[0],
        "decision_brief": ticker_decision_brief(normalized_ticker, tables),
        "found": any(tables.values()),
    }
