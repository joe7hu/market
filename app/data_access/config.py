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
    """Repository root (one level above the ``app/`` package).

    ``project_root()`` resolves to the ``app/`` package directory, but
    ``config.yaml`` and the default ``data/`` directory live at the repo root.
    Relative paths from config must resolve here so the app process reads and
    writes the same DuckDB file the read path (which resolves against the repo
    root via ``Path.cwd()``) uses — otherwise writes land in a stray
    ``app/data/investment.duckdb`` while reads serve the real one.
    """
    return project_root().parent




def _database_path(config: dict[str, Any]) -> Path:
    db_path = Path(config.get("database", {}).get("duckdb_path", "data/investment.duckdb"))
    return db_path if db_path.is_absolute() else repo_root() / db_path




def database_path(config: dict[str, Any]) -> Path:
    return _database_path(config)


def database_url(config: dict[str, Any]) -> str:
    return str(config.get("database", {}).get("url") or "postgresql:///market")




def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load config.yaml when PyYAML is installed; fall back to sensible defaults."""

    config_path = Path(path) if path else repo_root() / "config.yaml"
    defaults: dict[str, Any] = {
        "database": {"url": "postgresql:///market", "duckdb_path": "data/investment.duckdb"},
        "nas": {
            "source_root": "/Volumes/agent/data-sources",
            "status_dir": "/Volumes/agent/data-sources/status",
            "market_dir": "/Volumes/agent/data-sources/market-mini",
            "duckdb_snapshot_dir": "/Volumes/agent/data-sources/market-mini/duckdb-snapshots",
        },
        "arco": {"raw_dir": "/Volumes/agent/brain/raw/sources/arco"},
        "trader_profile_dir": "data/trader_profiles",
        "prompt_dir": "prompts",
    }
    if not config_path.exists():
        return _apply_runtime_overrides(defaults)

    try:
        import yaml
    except ModuleNotFoundError:
        return _apply_runtime_overrides(defaults | {"config_warning": "Install PyYAML to read config.yaml."})

    with config_path.open("r", encoding="utf-8") as handle:
        parsed = yaml.safe_load(handle) or {}
    return _apply_runtime_overrides(_deep_merge(defaults, parsed))




def _apply_runtime_overrides(config: dict[str, Any]) -> dict[str, Any]:
    database_url_override = os.environ.get("MARKET_DATABASE_URL")
    duckdb_path = os.environ.get("MARKET_DUCKDB_PATH")
    updated = config
    if database_url_override:
        updated = _deep_merge(updated, {"database": {"url": database_url_override}})
        updated.setdefault("runtime_overrides", {})["MARKET_DATABASE_URL"] = database_url_override
    if duckdb_path:
        updated = _deep_merge(updated, {"database": {"duckdb_path": duckdb_path}})
        updated.setdefault("runtime_overrides", {})["MARKET_DUCKDB_PATH"] = duckdb_path
    return updated




def tables_for_scope(scope: str) -> tuple[str, ...]:
    return contract_tables_for_scope(scope)
