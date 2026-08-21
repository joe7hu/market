"""FastAPI dependency providers and configuration normalization."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, Request

from app.actions.agents import AgentActions
from app.actions.options import OptionsActions
from app.actions.portfolio import PortfolioActions
from app.actions.sources import SourceActions
from app.actions.superinvestors import SuperinvestorActions
from app.actions.theses import ThesisActions
from app import job_control
from app.request_security import require_local_request
from investment_panel.core.config import AppConfig, load_config, public_config_payload as _public_config_payload
from investment_panel.database.authority import database_url, runtime_for_config


def get_config() -> AppConfig:
    """Load the typed application configuration once for a request/workflow."""

    return load_config()


def public_config(path: str | Path | None = None) -> dict[str, Any]:
    """Return the redacted public settings shape at the HTTP boundary."""

    return _public_config_payload(load_config(path))


def get_runtime(config: AppConfig = Depends(get_config)):
    """Construct the cached PostgreSQL runtime for one request."""

    return runtime_for_config(config)


def get_authorized_request(request: Request) -> Request:
    """Authorize a local write request through FastAPI dependency injection."""

    require_local_request(request)
    return request


def get_agent_actions(config: AppConfig = Depends(get_config)) -> AgentActions:
    return AgentActions(config, job_control.start_refresh_job)


def get_options_actions(config: AppConfig = Depends(get_config)) -> OptionsActions:
    return OptionsActions(config)


def get_portfolio_actions(config: AppConfig = Depends(get_config)) -> PortfolioActions:
    from app.data_access import mutations

    return PortfolioActions(
        config,
        save_watchlist=mutations.save_watchlist_symbol,
        populate_watchlist=mutations.populate_watchlist_symbol_data,
        delete_watchlist=mutations.delete_watchlist_symbol,
    )


def get_source_actions(config: AppConfig = Depends(get_config)) -> SourceActions:
    return SourceActions(config)


def get_superinvestor_actions(config: AppConfig = Depends(get_config)) -> SuperinvestorActions:
    return SuperinvestorActions(config)


def get_thesis_actions(config: AppConfig = Depends(get_config)) -> ThesisActions:
    return ThesisActions(config)


def public_config_payload(config: AppConfig) -> dict[str, Any]:
    """Convert typed configuration to a redacted public payload boundary."""

    return _public_config_payload(config)


__all__ = [
    "AppConfig",
    "database_url",
    "get_config",
    "get_runtime",
    "get_authorized_request",
    "get_agent_actions",
    "get_options_actions",
    "get_portfolio_actions",
    "get_source_actions",
    "get_superinvestor_actions",
    "get_thesis_actions",
    "load_config",
    "public_config",
    "public_config_payload",
    "runtime_for_config",
]
