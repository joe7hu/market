"""Explicit application compatibility surface for existing routers.

New code should import from the owning module directly:
``app.dependencies``, ``app.panel_snapshot``, ``app.job_control``,
``app.request_security``, or the domain data/action owner. This module remains
small and static so existing route names can migrate without another dynamic
facade.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.contracts import (
    AgentAnalyzeInput,
    AgentSettingsInput,
    OptionPaperEntryInput,
    OptionsHistoryToggleInput,
    PortfolioPositionInput,
    PortfolioTransactionInput,
    PortfolioTransactionReversalInput,
    ResearchSourcesInput,
    StrategyPromotionInput,
    ThesisAutomationInput,
    ThesisInput,
    ThesisReviewInput,
    TradeJournalInput,
    WatchlistSymbolInput,
)
from app.data_access import (
    dashboard_payload,
    delete_watchlist_symbol,
    load_daily_research_panel_data,
    load_market_panel_data,
    load_panel_data,
    load_panel_scope_data,
    load_table_panel_data,
    load_table_panel_page,
    load_ticker_panel_data,
    load_watchlist_scope_data,
    mark_thesis_reviewed,
    options_radar_rows,
    panel_contract_payload,
    panel_snapshot_payload,
    persist_setting_section,
    populate_watchlist_symbol_data,
    portfolio_correlation_rows,
    portfolio_exposure_rows,
    portfolio_performance_rows,
    portfolio_review_action_rows,
    portfolio_risk_rows,
    portfolio_rows,
    portfolio_summary,
    portfolio_transaction_rows,
    preview_portfolio_transaction,
    record_portfolio_transaction,
    record_thesis_review,
    reverse_portfolio_transaction,
    save_thesis,
    save_watchlist_symbol,
    settings_payload,
    signals_payload,
    status_payload,
    table_payload,
    thesis_history,
    thesis_monitor_payload,
    thesis_monitor_rows,
    thesis_rows,
    ticker_payload,
    update_agent_settings_config,
    update_research_sources_config,
    user_state_table_payload,
    watchlist_rows,
)
from app.data_access.config import load_config
from app.dependencies import database_url, runtime_for_config
from app.job_control import (
    ALLOWLIST,
    execute_background_refresh_job,
    execute_refresh_job,
    execute_refresh_job_subprocess,
    execute_thesis_monitor_automation,
    refresh_job_rows,
    run_refresh_job,
    start_refresh_job,
)
from app.panel_snapshot import (
    CONTEXT_CACHE_TTL_SECONDS,
    PANEL_SNAPSHOT_CONTRACT_REVISION,
    SOURCE_FRESHNESS_DEFAULT_LIMIT,
    _CONTEXT_LOCK,
    _LAST_GOOD_SCOPE_SNAPSHOTS,
    _scope_snapshot_cache_path,
    context as _panel_context,
    full_market_refresh_status,
    invalidate_context_cache,
    scope_snapshot_payload,
    table_payload_for,
    with_data_freshness,
)
from app.request_security import TAILSCALE_CGNAT, require_local_request
from fastapi import Request
from investment_panel.core.daily_research_prompt import build_daily_research_prompt
from investment_panel.database.migrations import HEAD_REVISION
from investment_panel.database.options_constants import DEFAULT_STRATEGY_VERSION
from investment_panel.database.storage_archive import StorageArchiveService


APP_TITLE = "Personal Investment Panel"


def storage_health(config: Any) -> dict[str, Any]:
    """Read archive health without loading any decision/read-model tables."""

    nas = config.get("nas") if isinstance(config, dict) else getattr(config, "nas", None)
    archive_dir = (
        (nas or {}).get("storage_archive_dir")
        if isinstance(nas, dict)
        else getattr(nas, "storage_archive_dir", None)
    )
    return StorageArchiveService(
        runtime_for_config(config),
        Path(archive_dir or "/Volumes/agent/data-sources/market-mini/storage-archive/v1"),
    ).health()


def _panel_snapshot_contract_revision() -> str:
    return PANEL_SNAPSHOT_CONTRACT_REVISION


def _context(cache_key: str = "full", loader: Callable[[dict[str, Any]], Any] | None = None) -> tuple[dict[str, Any], Any]:
    """Compatibility entry point with dependencies injected from this seam."""

    return _panel_context(
        cache_key,
        loader,
        config_loader=load_config,
        database_url_loader=database_url,
        panel_loader=load_panel_data,
    )


def _table_payload(table_name: str) -> dict[str, Any]:
    return table_payload_for(
        table_name,
        config_loader=load_config,
        database_url_loader=database_url,
        table_loader=load_table_panel_data,
    )


def _capped_table_payload(table_name: str, limit: int) -> dict[str, Any]:
    payload = _table_payload(table_name)
    rows = payload["rows"]
    safe_limit = max(1, min(int(limit or SOURCE_FRESHNESS_DEFAULT_LIMIT), 500))
    capped_rows = rows[:safe_limit]
    return {**payload, "rows": capped_rows, "count": len(rows), "returned_count": len(capped_rows), "limit": safe_limit}


def _execute_background_refresh_job(job_id: str, job_name: str, database_url_value: str) -> None:
    execute_background_refresh_job(job_id, job_name, database_url_value)


def _execute_thesis_monitor_automation(symbols: list[str], *, dry_run: bool, force: bool) -> None:
    execute_thesis_monitor_automation(symbols, dry_run=dry_run, force=force)


def _invalidate_context_cache() -> None:
    invalidate_context_cache()


def _full_market_refresh_status(config: dict[str, Any]) -> dict[str, Any] | None:
    return full_market_refresh_status(config)


def _with_data_freshness(payload: dict[str, Any]) -> dict[str, Any]:
    return with_data_freshness(payload)


def _require_local_request(request: Request) -> None:
    require_local_request(request)


def scope_panel_snapshot_payload(
    config: dict[str, Any],
    panel_data: Any,
    scope: str,
    *,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    return scope_snapshot_payload(config, panel_data, scope, offset=offset, limit=limit)


__all__ = [
    "ALLOWLIST",
    "APP_TITLE",
    "AgentAnalyzeInput",
    "AgentSettingsInput",
    "CONTEXT_CACHE_TTL_SECONDS",
    "DEFAULT_STRATEGY_VERSION",
    "HEAD_REVISION",
    "OptionPaperEntryInput",
    "OptionsHistoryToggleInput",
    "PANEL_SNAPSHOT_CONTRACT_REVISION",
    "PortfolioPositionInput",
    "PortfolioTransactionInput",
    "PortfolioTransactionReversalInput",
    "ResearchSourcesInput",
    "SOURCE_FRESHNESS_DEFAULT_LIMIT",
    "StrategyPromotionInput",
    "TAILSCALE_CGNAT",
    "ThesisAutomationInput",
    "ThesisInput",
    "ThesisReviewInput",
    "TradeJournalInput",
    "WatchlistSymbolInput",
    "_CONTEXT_LOCK",
    "_LAST_GOOD_SCOPE_SNAPSHOTS",
    "_capped_table_payload",
    "_context",
    "_execute_background_refresh_job",
    "_execute_thesis_monitor_automation",
    "_full_market_refresh_status",
    "_invalidate_context_cache",
    "_panel_snapshot_contract_revision",
    "_require_local_request",
    "_scope_snapshot_cache_path",
    "_table_payload",
    "_with_data_freshness",
    "build_daily_research_prompt",
    "dashboard_payload",
    "database_url",
    "delete_watchlist_symbol",
    "execute_refresh_job",
    "execute_refresh_job_subprocess",
    "load_config",
    "load_daily_research_panel_data",
    "load_market_panel_data",
    "load_panel_data",
    "load_panel_scope_data",
    "load_table_panel_page",
    "load_ticker_panel_data",
    "load_watchlist_scope_data",
    "mark_thesis_reviewed",
    "options_radar_rows",
    "panel_contract_payload",
    "panel_snapshot_payload",
    "persist_setting_section",
    "populate_watchlist_symbol_data",
    "portfolio_correlation_rows",
    "portfolio_exposure_rows",
    "portfolio_performance_rows",
    "portfolio_review_action_rows",
    "portfolio_risk_rows",
    "portfolio_rows",
    "portfolio_summary",
    "portfolio_transaction_rows",
    "preview_portfolio_transaction",
    "record_portfolio_transaction",
    "record_thesis_review",
    "refresh_job_rows",
    "reverse_portfolio_transaction",
    "run_refresh_job",
    "save_thesis",
    "save_watchlist_symbol",
    "scope_panel_snapshot_payload",
    "settings_payload",
    "signals_payload",
    "start_refresh_job",
    "status_payload",
    "storage_health",
    "table_payload",
    "thesis_history",
    "thesis_monitor_payload",
    "thesis_monitor_rows",
    "thesis_rows",
    "ticker_payload",
    "update_agent_settings_config",
    "update_research_sources_config",
    "user_state_table_payload",
    "watchlist_rows",
]
