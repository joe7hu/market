"""Resolve and cache the process PostgreSQL authority."""

from __future__ import annotations

from threading import RLock
from typing import Any

from investment_panel.database.runtime import DatabaseRuntime


def database_url(config: Any) -> str:
    if isinstance(config, dict):
        value = (config.get("database") or {}).get("url")
    else:
        database = getattr(config, "database", None)
        value = getattr(database, "url", None)
    dsn = str(value or "postgresql:///market")
    if not dsn.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ValueError("database.url must identify PostgreSQL")
    return dsn


_RUNTIMES: dict[str, DatabaseRuntime] = {}
_RUNTIMES_LOCK = RLock()


def runtime_for_url(dsn: str) -> DatabaseRuntime:
    with _RUNTIMES_LOCK:
        runtime = _RUNTIMES.get(dsn)
        if runtime is None:
            runtime = DatabaseRuntime(dsn)
            runtime.open()
            _RUNTIMES[dsn] = runtime
        return runtime


def runtime_for_config(config: Any) -> DatabaseRuntime:
    return runtime_for_url(database_url(config))


def close_cached_runtimes() -> None:
    with _RUNTIMES_LOCK:
        runtimes = tuple(_RUNTIMES.values())
        _RUNTIMES.clear()
    for runtime in runtimes:
        runtime.close()
