"""API payload builders for panel views."""

from __future__ import annotations
from typing import Any
from app.scheduler import scheduler_status
from investment_panel.core.panel import (
    build_ticker_dossier,
    dashboard_payload as core_dashboard_payload,
    panel_contract_payload as contract_panel_payload,
    panel_snapshot_payload as core_panel_snapshot_payload,
)

from app.data_access.types import PanelData
from app.data_access.coerce import int_value as _int_value, jsonable
from investment_panel.core.agent_config import ThesisMonitorAgentConfig
from investment_panel.core.config import AppConfig, OptionAgentConfig
from investment_panel.core.decision import ticker_decision_brief

DEFAULT_AGENT_THESIS_REQUEST_LIMIT = 12



def status_payload(panel_data: PanelData) -> dict[str, Any]:
    return {
        "ready": panel_data.status.ready,
        "message": panel_data.status.message,
        "source": panel_data.status.source,
        "metadata": jsonable(panel_data.metadata),
    }




def runtime_metadata(config: AppConfig) -> dict[str, Any]:
    agents = config.agents
    option_agent = agents.option_agent
    thesis_monitor = agents.thesis_monitor
    return {
        "agents": {
            # Unified single-pass agent runtime. Sub-limits keep thesis/postmortem
            # counts visible even though one consolidated call covers both.
            "option_agent": _agent_runtime_metadata(option_agent, default_limit=8) | {
                "thesis_limit": _int_value(option_agent.thesis_limit, 8),
                "postmortem_limit": _int_value(option_agent.postmortem_limit, 4),
                "request_cap": DEFAULT_AGENT_THESIS_REQUEST_LIMIT,
                "queue_policy": "current_ranked_candidates_plus_ondemand",
                "cadence": "launchd_weekdays_0815_or_ondemand",
                "schedule_owner": "launchd",
                "max_runs_per_day": _int_value(option_agent.max_runs_per_day, 1),
                "mode": "consolidated_single_pass",
            },
            "thesis_monitor": _thesis_monitor_runtime_metadata(thesis_monitor),
        },
        "options_radar": {
            "deterministic_cadence": "hourly",
            "agent_cadence": "daily_premarket",
        },
        "scheduler": scheduler_status(config),
    }




def _agent_runtime_metadata(
    config: OptionAgentConfig,
    *,
    default_limit: int,
) -> dict[str, Any]:
    command = str(config.command or "")
    enabled = bool(config.enabled)
    configured = bool(command.strip())
    return {
        "enabled": enabled,
        "configured": configured,
        "active": enabled and configured,
        "status": "active" if enabled and configured else "paused",
        "limit": _int_value(config.thesis_limit + config.postmortem_limit, default_limit),
        "timeout_seconds": _int_value(config.timeout_seconds, 120),
    }


def _thesis_monitor_runtime_metadata(config: ThesisMonitorAgentConfig) -> dict[str, Any]:
    enabled = bool(getattr(config, "enabled", False))
    provider = str(getattr(config, "provider", None) or "codex")
    model = str(getattr(config, "model", None) or "")
    configured_providers = {"codex", "deepseek"}
    return {
        "enabled": enabled,
        "configured": provider in configured_providers,
        "active": enabled,
        "status": "active" if enabled else "paused",
        "provider": provider,
        "model": model or "gpt-5.6-luna",
        "reasoning_effort": str(getattr(config, "reasoning_effort", None) or "high"),
        "prompt_version": str(getattr(config, "prompt_version", None) or "thesis_v3_20260725"),
        "concurrency": _int_value(getattr(config, "concurrency", None), 2),
        "evidence_items_per_symbol": _int_value(getattr(config, "evidence_items_per_symbol", None), 12),
        "cadence": "preopen_plus_material_events",
        "preopen_enabled": bool(getattr(config, "preopen_enabled", False)),
        "material_event_enabled": bool(getattr(config, "material_event_enabled", False)),
        "debounce_minutes": _int_value(getattr(config, "debounce_minutes", None), 30),
        "max_material_runs_per_symbol_per_day": _int_value(getattr(config, "max_material_runs_per_symbol_per_day", None), 2),
        "authority": "research_ranking_only",
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
    """Section-organized per-ticker dossier (the single authoritative API model).

    Loads the per-ticker read-model tables, synthesizes the decision brief, and
    composes both into one ``dossier`` of normalized sections (quote,
    fundamentals, estimates, technicals, options, ownership, sources, thesis,
    portfolio, decision) plus a coverage overview. Each section carries an
    explicit ``coverage.status`` so callers can degrade gracefully.
    """

    normalized_ticker = ticker.upper()
    tables = {
        name: [row for row in rows if _payload_symbol(row) in {"", normalized_ticker}]
        for name, rows in panel_data.tables.items()
    }
    decision_brief = ticker_decision_brief(normalized_ticker, tables)
    dossier = build_ticker_dossier(normalized_ticker, tables, decision_brief)
    return {
        "symbol": normalized_ticker,
        "ticker": normalized_ticker,
        "status": status_payload(panel_data),
        "as_of": dossier["coverage"].get("as_of"),
        "dossier": dossier,
        "found": bool(dossier["coverage"].get("present") or dossier["coverage"]["live"]),
    }


def _payload_symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("ticker") or "").upper()
