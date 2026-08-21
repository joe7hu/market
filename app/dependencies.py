"""FastAPI dependency providers and configuration normalization."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.data_access.config import database_url, load_config as load_public_config
from investment_panel.core.config import AppConfig, config_to_dict, load_config as load_typed_config
from investment_panel.database.authority import runtime_for_config


def get_config(path: str | Path | None = None) -> AppConfig:
    """Load the typed application configuration once for a request/workflow."""

    return load_typed_config(path)


def public_config(path: str | Path | None = None) -> dict[str, Any]:
    """Return the legacy public settings shape at the HTTP boundary only."""

    return load_public_config(path)


def public_config_payload(config: AppConfig) -> dict[str, Any]:
    """Convert typed configuration to a redacted public payload boundary."""

    return config_to_dict(config)


__all__ = [
    "AppConfig",
    "database_url",
    "get_config",
    "load_typed_config",
    "public_config",
    "public_config_payload",
    "runtime_for_config",
]
