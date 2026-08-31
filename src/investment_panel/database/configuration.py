"""PostgreSQL authority configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime


@dataclass(frozen=True)
class DatabaseConfig:
    url: str = "postgresql:///market"


def load_database_config(raw: dict[str, Any], base: object | None = None) -> DatabaseConfig:
    values = raw.get("database", {})
    url = str(os.environ.get("MARKET_DATABASE_URL") or values.get("url") or "postgresql:///market")
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ValueError("database.url must identify PostgreSQL")
    return DatabaseConfig(url=url)


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


def persisted_setting_sections(database_url: str) -> dict[str, Any]:
    """Read DB setting overrides without making config loading depend on PostgreSQL."""

    try:
        from investment_panel.database.authority import runtime_for_url

        return SettingRepository(runtime_for_url(database_url)).sections()
    except Exception:
        return {}


__all__ = [
    "DatabaseConfig",
    "SettingRepository",
    "load_database_config",
    "persisted_setting_sections",
]
