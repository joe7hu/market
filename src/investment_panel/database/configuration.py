"""PostgreSQL authority configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DatabaseConfig:
    url: str = "postgresql:///market"
    duckdb_path: Path = Path(__file__).resolve().parents[3] / "data" / "investment.duckdb"


def load_database_config(raw: dict[str, Any], base: Path) -> DatabaseConfig:
    values = raw.get("database", {})
    url = str(os.environ.get("MARKET_DATABASE_URL") or values.get("url") or "postgresql:///market")
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ValueError("database.url must identify PostgreSQL")
    legacy_path = Path(os.path.expandvars(os.environ.get("MARKET_DUCKDB_PATH") or values.get("duckdb_path", "data/investment.duckdb"))).expanduser()
    if not legacy_path.is_absolute():
        legacy_path = base / legacy_path
    return DatabaseConfig(url=url, duckdb_path=legacy_path)
