"""Small normalization helpers for canonical source ingestion."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from investment_panel.core.instruments import normalize_symbol

SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,14}$")

def symbols_from_value(value: Any) -> list[str]:
    parsed = parse_json(value)
    if isinstance(parsed, list):
        return sorted({symbol for item in parsed for symbol in [normalize_signal_symbol(item)] if symbol})
    if isinstance(parsed, str):
        return sorted({symbol for item in re.split(r"[,;\s]+", parsed) for symbol in [normalize_signal_symbol(item)] if symbol})
    return []


def normalize_signal_symbol(value: Any) -> str:
    symbol = normalize_symbol(str(value or ""))
    return symbol if symbol and SYMBOL_RE.match(symbol) else ""


def evidence_refs_from_claims(claims: Any, fallback_url: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(claims, dict):
        for item in claims.get("evidence", []) if isinstance(claims.get("evidence"), list) else []:
            if isinstance(item, dict):
                refs.extend(str(item.get(key)) for key in ("url", "source_url", "ref") if item.get(key))
            elif item:
                refs.append(str(item))
    if fallback_url:
        refs.append(str(fallback_url))
    return sorted(set(refs))


def source_row_freshness(row: dict[str, Any]) -> str:
    status = str(row.get("latest_run_status") or "").lower()
    if row.get("enabled") is False:
        return "disabled"
    if status in {"failed", "error"}:
        return "failed"
    if status in {"not_loaded", "configured"}:
        return "not_loaded"
    if row.get("items_count") or row.get("signals_count"):
        return "loaded"
    if row.get("latest_run_at"):
        return "checked"
    return "not_loaded"


def parse_json(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def decode_row(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for key in ("config", "tickers", "evidence_refs", "raw", "catalysts", "risks"):
        if key in decoded:
            decoded[key] = parse_json(decoded[key])
    return decoded


def infer_sentiment(value: Any) -> str:
    text = str(value or "").lower()
    if any(term in text for term in ("risk", "bear", "decline", "miss", "sell", "short", "weak")):
        return "bearish"
    if any(term in text for term in ("buy", "bull", "growth", "beat", "thesis", "strong", "upside")):
        return "bullish"
    return "neutral"


def stable_id(*parts: Any) -> str:
    joined = "|".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def slug(value: Any) -> str:
    text = str(value or "source").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "source"
