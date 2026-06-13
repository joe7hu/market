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




def _database_path(config: dict[str, Any]) -> Path:
    db_path = Path(config.get("database", {}).get("duckdb_path", "data/investment.duckdb"))
    return db_path if db_path.is_absolute() else project_root() / db_path




def database_path(config: dict[str, Any]) -> Path:
    return _database_path(config)




def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load config.yaml when PyYAML is installed; fall back to sensible defaults."""

    config_path = Path(path) if path else project_root() / "config.yaml"
    defaults: dict[str, Any] = {
        "database": {"duckdb_path": "data/investment.duckdb"},
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
    duckdb_path = os.environ.get("MARKET_DUCKDB_PATH")
    if not duckdb_path:
        return config
    updated = _deep_merge(config, {"database": {"duckdb_path": duckdb_path}})
    updated.setdefault("runtime_overrides", {})["MARKET_DUCKDB_PATH"] = duckdb_path
    return updated




def tables_for_scope(scope: str) -> tuple[str, ...]:
    return contract_tables_for_scope(scope)
