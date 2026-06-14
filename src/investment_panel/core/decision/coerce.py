"""JSON/date parsing and row coercion helpers."""

from __future__ import annotations
from datetime import UTC, datetime
from typing import Any

from investment_panel.core.coercion import parse_dt_utc as parse_dt
from investment_panel.core.coercion import parse_json



def latest_by_symbol(rows: list[dict[str, Any]], symbol_key: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get(symbol_key) or "").upper()
        if symbol and symbol not in result:
            result[symbol] = row
    return result




def dedupe_freshness(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["source_key"]
        existing = deduped.get(key)
        if not existing or (parse_dt(row.get("last_observed_at")) or datetime.min.replace(tzinfo=UTC)) >= (parse_dt(existing.get("last_observed_at")) or datetime.min.replace(tzinfo=UTC)):
            deduped[key] = row
    return list(deduped.values())




def related_symbols(value: Any) -> list[str]:
    parsed = parse_json(value)
    if isinstance(parsed, list):
        return [str(item).split(":")[-1].upper() for item in parsed]
    if isinstance(value, str):
        return [item.strip().split(":")[-1].upper() for item in value.replace(";", ",").split(",") if item.strip()]
    return []




def recency_points(observed: datetime) -> float:
    age_days = max(0.0, (datetime.now(UTC) - observed).total_seconds() / 86400)
    return max(0.0, 100.0 - age_days * 10)




def decode(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for field in ("inclusion_reasons", "source_counts", "blocking_gates", "decision_basis", "portfolio_impact", "snapshot"):
        if field in decoded:
            decoded[field] = parse_json(decoded[field])
    if "latest_source_timestamp" in decoded:
        decoded["latest_source_at"] = decoded["latest_source_timestamp"]
    if "source_counts" in decoded and "source_count" not in decoded:
        counts = decoded.get("source_counts") or {}
        decoded["source_count"] = sum(int(value or 0) for value in counts.values()) if isinstance(counts, dict) else 0
    if "source_key" in decoded:
        decoded["source"] = decoded["source_key"]
        decoded["source_kind"] = "documentation" if decoded.get("docs_only") else decoded.get("source_type")
        decoded["provider_status"] = decoded.get("status")
    snapshot = decoded.get("snapshot")
    if isinstance(snapshot, dict) and "invalidation" in snapshot:
        decoded["invalidation"] = snapshot.get("invalidation")
    return decoded
