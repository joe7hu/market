"""PostgreSQL authority configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime


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


class SettingRepository:
    """Small JSON settings store; secrets remain environment-owned."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def sections(self, keys: tuple[str, ...] = ("agents", "research_sources")) -> dict[str, Any]:
        with self.runtime.read() as connection:
            rows = connection.execute(
                "SELECT key, value FROM app.setting WHERE key = ANY(%s)", [list(keys)]
            ).fetchall()
        return {str(row["key"]): dict(row["value"] or {}) for row in rows}

    def set_section(self, key: str, value: dict[str, Any]) -> None:
        if key not in {"agents", "research_sources"}:
            raise ValueError(f"setting section is not writable: {key}")
        with self.runtime.transaction() as connection:
            connection.execute(
                """
                INSERT INTO app.setting (key, value, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                [key, Jsonb(value)],
            )


def merge_persisted_setting_sections(raw: dict[str, Any], database_url: str) -> dict[str, Any]:
    """Overlay DB settings while keeping initial migration config loadable."""

    try:
        from investment_panel.database.authority import runtime_for_url

        sections = SettingRepository(runtime_for_url(database_url)).sections()
    except Exception:
        return raw
    return _merge(raw, sections)


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged
