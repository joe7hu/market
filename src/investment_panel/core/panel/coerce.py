"""Generic numeric, text, symbol, and JSON coercion helpers."""

from __future__ import annotations
import json
import re
from typing import Any



def _median(values: list[float | None]) -> float | None:
    cleaned = sorted(value for value in values if value is not None and value == value)
    if not cleaned:
        return None
    mid = len(cleaned) // 2
    if len(cleaned) % 2:
        return round(cleaned[mid], 4)
    return round((cleaned[mid - 1] + cleaned[mid]) / 2, 4)




def _percentile_rank(values: list[float | None], current: float | None) -> float | None:
    cleaned = sorted(value for value in values if value is not None and value == value)
    if current is None or len(cleaned) < 2:
        return None
    below = sum(1 for value in cleaned if value < current)
    return round((below / (len(cleaned) - 1)) * 100, 2)




def _optional_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    number = _number_from_any(value)
    return number if number == number else None




def _average(values: list[float | None]) -> float | None:
    cleaned = [value for value in values if value is not None and value == value]
    return round(sum(cleaned) / len(cleaned), 4) if cleaned else None




def _share(values: list[bool]) -> float:
    return sum(1 for value in values if value) / len(values) if values else 0.0




def _format_metric(value: float | None, unit: str) -> str:
    if value is None:
        return "n/a"
    if unit == "$":
        return f"${value / 1_000_000:.1f}M" if abs(value) >= 1_000_000 else f"${value:,.0f}"
    if unit == "%":
        return f"{value:+.1f}%"
    if unit == "x":
        return f"{value:.1f}x"
    return f"{value:.1f}"




def _last_history_close(history: list[dict[str, Any]]) -> float:
    for point in reversed(history):
        close = _number_from_any(point.get("close"))
        if close:
            return close
    return 0.0




def _symbols_from_value(value: Any) -> list[str]:
    symbols = []
    for item in _string_list(value):
        symbol = _normalize_symbol_token(item)
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols




def _symbols_from_text(value: Any, known_symbols: set[str]) -> list[str]:
    text = str(value or "").upper()
    symbols = []
    for symbol in sorted(known_symbols, key=len, reverse=True):
        if not symbol or len(symbol) < 2:
            continue
        if re.search(rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])", text):
            symbols.append(symbol)
    return symbols




def _is_generic_source_signal(row: dict[str, Any]) -> bool:
    signal_type = str(row.get("signal_type") or "")
    if signal_type in {"earnings_event", "analyst_estimate"}:
        return True
    title = str(row.get("title") or "").strip()
    thesis = str(row.get("thesis") or "").strip()
    antithesis = str(row.get("antithesis") or "").strip()
    return bool(thesis and title == thesis and antithesis.startswith("No structured"))




def _normalize_symbol_token(value: Any) -> str:
    text = str(value or "").strip().strip('"').strip("'").upper()
    if not text:
        return ""
    if ":" in text:
        text = text.split(":")[-1]
    if text.startswith("$") or text.startswith("#"):
        text = text[1:]
    normalized = "".join(char for char in text if char.isalnum() or char in {".", "-"})
    return normalized.strip(".-")




def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        return [str(item).strip() for item in value.values() if str(item).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("[") or stripped.startswith("{") or stripped.startswith('"'):
            try:
                return _string_list(json.loads(stripped))
            except Exception:
                pass
        return [item.strip() for item in stripped.replace("|", ";").replace(",", ";").split(";") if item.strip()]
    return [str(value).strip()] if str(value).strip() else []




def _dict_from_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}




def _date_text(value: Any) -> str:
    if not value:
        return ""
    return str(value)[:10]




def _source_label(value: Any, fallback: str) -> str:
    items = _string_list(value)
    if items:
        return " + ".join(items[:2])
    return fallback.replace("_", " ")




def _plain_text(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, dict):
        return "; ".join(str(item).strip() for item in value.values() if str(item).strip())
    return str(value or "").strip()




def _number_from_any(value: Any) -> float:
    if isinstance(value, (int, float)) and value == value:
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace("$", "").replace(",", "").replace("%", ""))
        except ValueError:
            return 0.0
    return 0.0




def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or not denominator:
        return None
    return numerator / denominator




def _meaningful_text(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text in {"", "-", "none", "None", "null", "N/A", "n/a"} else text




def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)




def first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None




def decode_fields(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    decoded = dict(row)
    for field in fields:
        if field in decoded:
            try:
                decoded[field] = decode_json_value(decoded[field])
            except Exception:
                pass
    return decoded




def decode_json_value(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)
