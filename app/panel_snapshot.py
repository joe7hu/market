"""Panel snapshot owner.

This module owns read-model cache lifetime, pagination metadata, and freshness
markers. It does not own SQL or write actions.
"""

from __future__ import annotations

import inspect
import json
from hashlib import sha256
from threading import Event, RLock
import time
from typing import Any, Callable

from fastapi import HTTPException

from app.data_access import loaders as loaders_owner
from app.data_access.payloads import panel_snapshot_payload, table_payload
from investment_panel.core.config import AppConfig, load_config
from investment_panel.core.panel import PANEL_SCOPE_TABLES, SCOPED_TABLE_COMPACT_FIELDS, SCOPED_TABLE_ROW_LIMITS
from investment_panel.database.authority import database_url


class _ContextFlight:
    def __init__(self) -> None:
        self.event = Event()
        self.error: BaseException | None = None


CONTEXT_CACHE_TTL_SECONDS = 3.0
CONTEXT_CACHE_MAX_ENTRIES = 32
SOURCE_FRESHNESS_DEFAULT_LIMIT = 100
_CONTEXT_CACHE: dict[str, Any] = {"entries": {}}
_CONTEXT_LOCK = RLock()
_CONTEXT_INFLIGHT: dict[tuple[str, str], _ContextFlight] = {}
_HOUSEKEEPING_REFRESH_STEPS = frozenset({"retention_prune", "database_snapshot"})


def panel_snapshot_contract_revision() -> str:
    contract = {
        "scopes": {scope: list(tables) for scope, tables in sorted(PANEL_SCOPE_TABLES.items())},
        "limits": {scope: dict(sorted(limits.items())) for scope, limits in sorted(SCOPED_TABLE_ROW_LIMITS.items())},
        "compact_fields": {
            scope: {table: sorted(fields) for table, fields in sorted(tables.items())}
            for scope, tables in sorted(SCOPED_TABLE_COMPACT_FIELDS.items())
        },
    }
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()[:16]


PANEL_SNAPSHOT_CONTRACT_REVISION = panel_snapshot_contract_revision()


def context(
    cache_key: str = "full",
    loader: Callable[[dict[str, Any]], Any] | None = None,
    *,
    config_loader: Callable[[], AppConfig] | None = None,
    database_url_loader: Callable[[AppConfig], str] | None = None,
    panel_loader: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], Any]:
    active_config_loader = config_loader or load_config
    active_database_url_loader = database_url_loader or database_url
    active_panel_loader = panel_loader or loaders_owner.load_panel_data
    config = active_config_loader()
    config_key = active_database_url_loader(config)
    active_loader = loader or (lambda active_config: _load_panel_data_without_repairs(active_config, panel_loader=active_panel_loader))
    flight_key = (cache_key, config_key)
    while True:
        now = time.monotonic()
        with _CONTEXT_LOCK:
            entries = _CONTEXT_CACHE.setdefault("entries", {})
            _prune_context_entries(entries, now)
            cached = entries.get(cache_key)
            if cached is not None and cached.get("config_key") == config_key and now < float(cached.get("expires_at") or 0):
                return cached["value"]
            flight = _CONTEXT_INFLIGHT.get(flight_key)
            if flight is None:
                flight = _ContextFlight()
                _CONTEXT_INFLIGHT[flight_key] = flight
                break
        flight.event.wait()
        if flight.error is not None:
            raise flight.error

    try:
        value = (config, active_loader(config))
    except BaseException as exc:
        with _CONTEXT_LOCK:
            if _CONTEXT_INFLIGHT.get(flight_key) is flight:
                _CONTEXT_INFLIGHT.pop(flight_key, None)
            flight.error = exc
            flight.event.set()
        raise

    loaded_at = time.monotonic()
    with _CONTEXT_LOCK:
        if _CONTEXT_INFLIGHT.get(flight_key) is flight:
            entries = _CONTEXT_CACHE.setdefault("entries", {})
            _prune_context_entries(entries, loaded_at)
            entries.pop(cache_key, None)
            entries[cache_key] = {
                "value": value,
                "config_key": config_key,
                "expires_at": loaded_at + CONTEXT_CACHE_TTL_SECONDS,
            }
            _prune_context_entries(entries, loaded_at)
            _CONTEXT_INFLIGHT.pop(flight_key, None)
        flight.event.set()
        return value


def _prune_context_entries(entries: dict[str, Any], now: float) -> None:
    for key, cached in list(entries.items()):
        if now >= float(cached.get("expires_at") or 0):
            entries.pop(key, None)
    while len(entries) > CONTEXT_CACHE_MAX_ENTRIES:
        entries.pop(next(iter(entries)))


def _load_panel_data_without_repairs(active_config: AppConfig, *, panel_loader: Callable[..., Any]) -> Any:
    parameters = inspect.signature(panel_loader).parameters
    if "ensure_decision_models" not in parameters:
        return panel_loader(active_config)
    return panel_loader(active_config, ensure_decision_models=False, ensure_source_models=False)


def table_payload_for(
    table_name: str,
    *,
    config_loader: Callable[[], AppConfig] | None = None,
    database_url_loader: Callable[[AppConfig], str] | None = None,
    table_loader: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    active_table_loader = table_loader or loaders_owner.load_table_panel_data
    _, panel_data = context(
        cache_key=f"table:{table_name}",
        loader=lambda config: active_table_loader(config, table_name),
        config_loader=config_loader,
        database_url_loader=database_url_loader,
    )
    return table_payload(panel_data, table_name)


def scope_snapshot_payload(
    config: AppConfig,
    panel_data: Any,
    scope: str,
    *,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    payload = panel_snapshot_payload(panel_data, scope, offset=offset, limit=limit)
    status = payload.get("status")
    if isinstance(status, dict) and status.get("ready") is True:
        _mark_snapshot_state(payload, "current")
        return payload
    message = str(status.get("message") if isinstance(status, dict) else "") or "PostgreSQL read models unavailable."
    raise HTTPException(status_code=503, detail=message)


def _mark_snapshot_state(payload: dict[str, Any], state: str, *, error: str | None = None) -> None:
    status = dict(payload.get("status") or {})
    metadata = dict(status.get("metadata") or {})
    metadata["snapshot_state"] = state
    metadata["panel_contract_revision"] = PANEL_SNAPSHOT_CONTRACT_REVISION
    if error:
        metadata["snapshot_error"] = error
    status["metadata"] = metadata
    payload["status"] = status


def capped_table_payload(
    table_name: str,
    limit: int,
    *,
    table_payload_loader: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    payload = table_payload_loader(table_name)
    rows = payload["rows"]
    safe_limit = max(1, min(int(limit or SOURCE_FRESHNESS_DEFAULT_LIMIT), 500))
    capped_rows = rows[:safe_limit]
    return {**payload, "rows": capped_rows, "count": len(rows), "returned_count": len(capped_rows), "limit": safe_limit}


def invalidate_context_cache() -> None:
    with _CONTEXT_LOCK:
        _CONTEXT_CACHE["entries"] = {}
        flights = tuple(_CONTEXT_INFLIGHT.values())
        _CONTEXT_INFLIGHT.clear()
        for flight in flights:
            flight.event.set()


def full_market_refresh_status(config: AppConfig) -> dict[str, Any] | None:
    status_dir = config.nas.status_dir
    status_path = status_dir / "mini-market-full-refresh.json"
    if not status_path.exists():
        return None
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return with_data_freshness(payload)


def with_data_freshness(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("dataOk") is not None:
        return payload
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        return payload
    data_ok = all(step.get("ok") for step in steps if isinstance(step, dict) and step.get("name") not in _HOUSEKEEPING_REFRESH_STEPS)
    payload["dataOk"] = data_ok
    payload["dataFinishedAt"] = payload.get("finishedAt") if data_ok else None
    return payload


__all__ = [
    "CONTEXT_CACHE_TTL_SECONDS",
    "CONTEXT_CACHE_MAX_ENTRIES",
    "PANEL_SNAPSHOT_CONTRACT_REVISION",
    "SOURCE_FRESHNESS_DEFAULT_LIMIT",
    "capped_table_payload",
    "context",
    "full_market_refresh_status",
    "invalidate_context_cache",
    "panel_snapshot_contract_revision",
    "scope_snapshot_payload",
    "table_payload_for",
    "with_data_freshness",
]
