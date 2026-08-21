"""Panel snapshot owner.

This module owns cache lifetime, scoped last-good fallback, pagination metadata,
and freshness markers. It does not own SQL or write actions.
"""

from __future__ import annotations

from copy import deepcopy
import inspect
import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from threading import RLock
import time
from typing import Any, Callable

from fastapi import HTTPException

from app.data_access.config import database_url, load_config
from app.data_access.loaders import load_panel_data, load_table_panel_data
from app.data_access.payloads import panel_snapshot_payload, table_payload
from app.data_access.types import PanelData
from app.panel_contracts import PANEL_SCOPE_TABLES
from investment_panel.core.panel import SCOPED_TABLE_COMPACT_FIELDS, SCOPED_TABLE_ROW_LIMITS
from investment_panel.database.migrations import HEAD_REVISION


CONTEXT_CACHE_TTL_SECONDS = 3.0
SOURCE_FRESHNESS_DEFAULT_LIMIT = 100
_CONTEXT_CACHE: dict[str, Any] = {"entries": {}, "expires_at": 0.0, "config_key": None, "value": None}
_CONTEXT_LOCK = RLock()
_LAST_GOOD_SCOPE_SNAPSHOTS: dict[str, dict[str, Any]] = {}
_SCOPE_SNAPSHOT_FALLBACK_TABLES = {
    "today": {"daily_brief", "preopen_daily_brief", "portfolio", "decision_queue", "decision_truth", "event_decision_packets", "event_scout_events"},
    "watchlist": {"universe_screen", "manual_watchlist", "portfolio"},
    "watchlist-watched": {"universe_screen", "manual_watchlist", "portfolio"},
    "watchlist-unwatched": {"universe_screen", "manual_watchlist", "portfolio"},
    "portfolio": {"portfolio", "portfolio_summary", "portfolio_performance"},
    "research": {"research_packets", "theses", "thesis_monitor", "news"},
    "options-radar": {"option_radar_summary", "option_radar_opportunity", "decision_truth", "event_decision_packets", "event_scout_events"},
}
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
    config_loader: Callable[[], dict[str, Any]] = load_config,
    database_url_loader: Callable[[dict[str, Any]], str] = database_url,
    panel_loader: Callable[..., Any] = load_panel_data,
) -> tuple[dict[str, Any], Any]:
    config = config_loader()
    config_key = database_url_loader(config)
    now = time.monotonic()
    with _CONTEXT_LOCK:
        entries = _CONTEXT_CACHE.setdefault("entries", {})
        cached = entries.get(cache_key)
        if cached is not None and cached.get("config_key") == config_key and now < float(cached.get("expires_at") or 0):
            return cached["value"]

    active_loader = loader or (lambda active_config: _load_panel_data_without_repairs(active_config, panel_loader=panel_loader))
    value = (config, active_loader(config))

    with _CONTEXT_LOCK:
        entries = _CONTEXT_CACHE.setdefault("entries", {})
        entries[cache_key] = {"value": value, "config_key": config_key, "expires_at": now + CONTEXT_CACHE_TTL_SECONDS}
        if cache_key == "full":
            _CONTEXT_CACHE.update({"value": value, "config_key": config_key, "expires_at": now + CONTEXT_CACHE_TTL_SECONDS})
        return value


def _load_panel_data_without_repairs(active_config: dict[str, Any], *, panel_loader: Callable[..., Any] = load_panel_data) -> Any:
    parameters = inspect.signature(panel_loader).parameters
    if "ensure_decision_models" not in parameters:
        return panel_loader(active_config)
    return panel_loader(active_config, ensure_decision_models=False, ensure_source_models=False)


def table_payload_for(
    table_name: str,
    *,
    config_loader: Callable[[], dict[str, Any]] = load_config,
    database_url_loader: Callable[[dict[str, Any]], str] = database_url,
    table_loader: Callable[..., Any] = load_table_panel_data,
) -> dict[str, Any]:
    _, panel_data = context(
        cache_key=f"table:{table_name}",
        loader=lambda config: table_loader(config, table_name),
        config_loader=config_loader,
        database_url_loader=database_url_loader,
    )
    return table_payload(panel_data, table_name)


def scope_snapshot_payload(
    config: dict[str, Any],
    panel_data: Any,
    scope: str,
    *,
    offset: int = 0,
    limit: int | None = None,
    cache_path_loader: Callable[[dict[str, Any], str], Path] | None = None,
) -> dict[str, Any]:
    path_loader = cache_path_loader or _scope_snapshot_cache_path
    payload = panel_snapshot_payload(panel_data, scope, offset=offset, limit=limit)
    if scope not in _SCOPE_SNAPSHOT_FALLBACK_TABLES:
        return payload
    status = payload.get("status")
    if isinstance(status, dict) and status.get("ready") is True:
        _mark_snapshot_state(payload, "current")
        _store_last_good_scope_snapshot(
            config, scope, payload, offset=offset, limit=limit, cache_path_loader=path_loader,
        )
        return payload
    fallback = _load_last_good_scope_snapshot(
        config, scope, offset=offset, limit=limit, cache_path_loader=path_loader,
    )
    if fallback is None:
        message = str(status.get("message") if isinstance(status, dict) else "") or "No current or last-good snapshot is available."
        raise HTTPException(status_code=503, detail=message)
    status = dict(fallback.get("status") or {})
    captured_at = str(status.get("metadata", {}).get("last_good_at") or "") if isinstance(status.get("metadata"), dict) else ""
    status.update({"ready": True, "source": "panel-snapshot-cache", "message": f"Serving last-good {scope} data while PostgreSQL is unavailable."})
    fallback["status"] = status
    error_status = payload.get("status")
    error = error_status.get("message") if isinstance(error_status, dict) else "PostgreSQL read models unavailable."
    _mark_snapshot_state(fallback, "stale", error=str(error), last_good_at=captured_at)
    return fallback


def _scope_snapshot_has_rows(scope: str, payload: dict[str, Any]) -> bool:
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        return False
    for table_name in _SCOPE_SNAPSHOT_FALLBACK_TABLES.get(scope, set()):
        table = tables.get(table_name)
        rows = table.get("rows") if isinstance(table, dict) else None
        if isinstance(rows, list) and rows:
            return True
    return False


def _store_last_good_scope_snapshot(
    config: dict[str, Any], scope: str, payload: dict[str, Any], *, offset: int = 0,
    limit: int | None = None, cache_path_loader: Callable[[dict[str, Any], str], Path],
) -> None:
    snapshot = deepcopy(payload)
    _mark_snapshot_state(snapshot, "current", last_good_at=datetime.now().astimezone().isoformat())
    key = _scope_snapshot_cache_key(scope, offset, limit)
    _LAST_GOOD_SCOPE_SNAPSHOTS[key] = snapshot
    path = cache_path_loader(config, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_text(json.dumps(snapshot, ensure_ascii=False, default=str), encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        return


def _load_last_good_scope_snapshot(
    config: dict[str, Any], scope: str, *, offset: int = 0, limit: int | None = None,
    cache_path_loader: Callable[[dict[str, Any], str], Path],
) -> dict[str, Any] | None:
    key = _scope_snapshot_cache_key(scope, offset, limit)
    cached = _LAST_GOOD_SCOPE_SNAPSHOTS.get(key)
    if cached is not None:
        return deepcopy(cached) if _snapshot_schema_is_compatible(cached) else None
    path = cache_path_loader(config, key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or not _snapshot_schema_is_compatible(payload):
        return None
    _LAST_GOOD_SCOPE_SNAPSHOTS[key] = payload
    return deepcopy(payload)


def _snapshot_schema_is_compatible(payload: dict[str, Any]) -> bool:
    metadata = ((payload.get("status") or {}).get("metadata") or {})
    schema_revision = str(metadata.get("schema_revision") or "") if isinstance(metadata, dict) else ""
    contract_revision = str(metadata.get("panel_contract_revision") or "") if isinstance(metadata, dict) else ""
    return not schema_revision or (schema_revision == HEAD_REVISION and contract_revision == PANEL_SNAPSHOT_CONTRACT_REVISION)


def _scope_snapshot_cache_key(scope: str, offset: int, limit: int | None) -> str:
    suffix = f"-{offset}-{limit}" if offset or limit is not None else ""
    return f"{scope}{suffix}"


def _scope_snapshot_cache_path(config: dict[str, Any], cache_key: str) -> Path:
    del config
    return Path(__file__).resolve().parents[1] / "data" / "api-cache" / f"panel-snapshot-{cache_key}.json"


def _mark_snapshot_state(payload: dict[str, Any], state: str, *, error: str | None = None, last_good_at: str | None = None) -> None:
    status = dict(payload.get("status") or {})
    metadata = dict(status.get("metadata") or {})
    metadata["snapshot_state"] = state
    metadata["panel_contract_revision"] = PANEL_SNAPSHOT_CONTRACT_REVISION
    if last_good_at:
        metadata["last_good_at"] = last_good_at
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
    _CONTEXT_CACHE.update({"entries": {}, "expires_at": 0.0, "config_key": None, "value": None})


def full_market_refresh_status(config: dict[str, Any]) -> dict[str, Any] | None:
    status_dir = Path(config.get("nas", {}).get("status_dir", "/Volumes/agent/data-sources/status"))
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
    "PANEL_SNAPSHOT_CONTRACT_REVISION",
    "SOURCE_FRESHNESS_DEFAULT_LIMIT",
    "_CONTEXT_LOCK",
    "_LAST_GOOD_SCOPE_SNAPSHOTS",
    "_context",
    "_scope_snapshot_cache_path",
    "capped_table_payload",
    "context",
    "full_market_refresh_status",
    "invalidate_context_cache",
    "panel_snapshot_contract_revision",
    "scope_snapshot_payload",
    "table_payload_for",
    "with_data_freshness",
]


# Keep the old private spelling local to this module while the application
# seam moves to explicit names.
_context = context
