"""FastAPI dependency providers and configuration normalization."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, Request

from investment_panel.workflows.agents import AgentActions
from investment_panel.workflows.options import OptionsActions
from investment_panel.workflows.portfolio import PortfolioActions
from investment_panel.workflows.theses import ThesisActions
from investment_panel.workflows.tickers import TickerActions
from investment_panel.infrastructure.postgres.options_history import OptionsHistoryService
from investment_panel.infrastructure.postgres.options_research import OptionsResearchRepository
from investment_panel.infrastructure.postgres.options_execution import OptionsExecutionRepository
from investment_panel.infrastructure.postgres.options_decision_system import OptionsDecisionSystemRepository
from investment_panel.infrastructure.postgres.options_recovery_read import RecoveryReadRepository
from investment_panel.infrastructure.postgres.paper_workbench import PaperWorkbenchRepository
from investment_panel.infrastructure.postgres.research_workbench import ResearchWorkbenchRepository
from investment_panel.api import job_control
from investment_panel.api.request_security import require_local_request
from investment_panel.settings import AppConfig, load_config
from investment_panel.infrastructure.postgres.authority import runtime_for_config
from investment_panel.infrastructure.postgres.sources import SourceRepository
from investment_panel.infrastructure.postgres.storage_archive import StorageArchiveService
from investment_panel.infrastructure.postgres.superinvestor_portfolios import superinvestor_portfolios


def get_config() -> AppConfig:
    """Load the typed application configuration once for a request/workflow."""

    return load_config()


def get_runtime(config: AppConfig = Depends(get_config)):
    """Construct the cached PostgreSQL runtime for one request."""

    return runtime_for_config(config)


def get_authorized_request(request: Request) -> Request:
    """Authorize a local API request through FastAPI dependency injection."""

    require_local_request(request)
    return request


def get_agent_actions(config: AppConfig = Depends(get_config)) -> AgentActions:
    return AgentActions(config, job_control.start_refresh_job)


def get_options_actions(config: AppConfig = Depends(get_config)) -> OptionsActions:
    return OptionsActions(config)


def get_ticker_actions(config: AppConfig = Depends(get_config)) -> TickerActions:
    return TickerActions(config)


def get_portfolio_actions(config: AppConfig = Depends(get_config)) -> PortfolioActions:
    return PortfolioActions(config)


def get_source_repository(config: AppConfig = Depends(get_config)) -> SourceRepository:
    return SourceRepository(runtime_for_config(config))


def get_storage_archive_service(config: AppConfig = Depends(get_config)) -> StorageArchiveService:
    return StorageArchiveService(runtime_for_config(config), Path(config.nas.storage_archive_dir))


def get_superinvestor_query(
    config: AppConfig = Depends(get_config),
) -> Callable[[str], dict[str, Any] | None]:
    runtime = runtime_for_config(config)

    def query(investor_key: str) -> dict[str, Any] | None:
        with runtime.read() as connection:
            rows = superinvestor_portfolios(
                connection,
                investor_key=investor_key,
                include_holdings=True,
            )
        return rows[0] if rows else None

    return query


def get_thesis_actions(config: AppConfig = Depends(get_config)) -> ThesisActions:
    return ThesisActions(config)


def get_paper_workbench(config: AppConfig = Depends(get_config)) -> PaperWorkbenchRepository:
    return PaperWorkbenchRepository(runtime_for_config(config))


def get_research_workbench(config: AppConfig = Depends(get_config)) -> ResearchWorkbenchRepository:
    return ResearchWorkbenchRepository(runtime_for_config(config))


__all__ = [
    "get_options_history",
    "OptionsHistoryService",
    "get_options_research",
    "OptionsResearchRepository",
    "get_options_execution",
    "OptionsExecutionRepository",
    "get_options_decision_system",
    "OptionsDecisionSystemRepository",
    "get_options_recovery",
    "RecoveryReadRepository",

    "AppConfig",
    "get_config",
    "get_runtime",
    "get_authorized_request",
    "get_agent_actions",
    "get_options_actions",
    "get_ticker_actions",
    "get_portfolio_actions",
    "get_source_repository",
    "get_storage_archive_service",
    "get_superinvestor_query",
    "get_thesis_actions",
    "get_paper_workbench",
    "get_research_workbench",
    "load_config",
    "runtime_for_config",
]


def get_options_history(config: AppConfig = Depends(get_config)) -> OptionsHistoryService:
    return OptionsHistoryService(runtime_for_config(config), options_risk_sleeve_capital=config.analysis.options_decision_system.options_risk_sleeve_capital)


def get_options_research(config: AppConfig = Depends(get_config)) -> OptionsResearchRepository:
    return OptionsResearchRepository(runtime_for_config(config), config)


def get_options_execution(config: AppConfig = Depends(get_config)) -> OptionsExecutionRepository:
    return OptionsExecutionRepository(runtime_for_config(config), config)


def get_options_decision_system(config: AppConfig = Depends(get_config)) -> OptionsDecisionSystemRepository:
    return OptionsDecisionSystemRepository(runtime_for_config(config), mode=config.analysis.options_decision_system.mode)


def get_options_recovery(config: AppConfig = Depends(get_config)) -> RecoveryReadRepository:
    return RecoveryReadRepository(runtime_for_config(config), recovery_paper_actions_enabled=config.analysis.options_decision_system.recovery_paper_actions_enabled)
