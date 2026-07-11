"""Config loading and database-path resolution."""

from __future__ import annotations
import os
from pathlib import Path
from typing import Any, Iterable
from app.panel_contracts import (
    DECISION_REPAIR_TABLES,
    SOURCE_REPAIR_TABLES,
    TICKER_TABLES,
    panel_contract_payload as contract_panel_payload,
    tables_for_scope as contract_tables_for_scope,
)

from app.data_access.coerce import _deep_merge



def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def repo_root() -> Path:
    """Repository root (one level above the ``app/`` package)."""
    return project_root().parent




def _database_path(config: dict[str, Any]) -> Path:
    """Deprecated cache-root compatibility helper; never opened as a database."""

    return repo_root() / "data"




def database_path(config: dict[str, Any]) -> Path:
    return _database_path(config)


def database_url(config: dict[str, Any]) -> str:
    return str(config.get("database", {}).get("url") or "postgresql:///market")




def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load config.yaml when PyYAML is installed; fall back to sensible defaults."""

    config_path = Path(path) if path else repo_root() / "config.yaml"
    defaults: dict[str, Any] = {
        "database": {"url": "postgresql:///market"},
        "nas": {
            "source_root": "/Volumes/agent/data-sources",
            "status_dir": "/Volumes/agent/data-sources/status",
            "market_dir": "/Volumes/agent/data-sources/market-mini",
            "postgres_backup_dir": "/Volumes/agent/data-sources/market-mini/postgres-backups",
        },
        "arco": {"raw_dir": "/Volumes/agent/brain/raw/sources/arco"},
        "trader_profile_dir": "data/trader_profiles",
        "prompt_dir": "prompts",
    }
    if not config_path.exists():
        return _with_persisted_settings(_apply_runtime_overrides(defaults), enabled=bool(os.environ.get("MARKET_DATABASE_URL")))

    try:
        import yaml
    except ModuleNotFoundError:
        return _apply_runtime_overrides(defaults | {"config_warning": "Install PyYAML to read config.yaml."})

    with config_path.open("r", encoding="utf-8") as handle:
        parsed = yaml.safe_load(handle) or {}
    configured = _apply_runtime_overrides(_deep_merge(defaults, parsed))
    return _with_persisted_settings(
        configured,
        enabled=bool(os.environ.get("MARKET_DATABASE_URL") or (parsed.get("database") or {}).get("url")),
    )




def _apply_runtime_overrides(config: dict[str, Any]) -> dict[str, Any]:
    database_url_override = os.environ.get("MARKET_DATABASE_URL")
    updated = config
    if database_url_override:
        updated = _deep_merge(updated, {"database": {"url": database_url_override}})
        updated.setdefault("runtime_overrides", {})["MARKET_DATABASE_URL"] = database_url_override
    return updated


def _with_persisted_settings(config: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return config
    try:
        from investment_panel.database.authority import runtime_for_url
        from investment_panel.database.configuration import SettingRepository

        sections = SettingRepository(runtime_for_url(database_url(config))).sections()
    except Exception:
        return config
    return _deep_merge(config, sections)




def tables_for_scope(scope: str) -> tuple[str, ...]:
    return contract_tables_for_scope(scope)
