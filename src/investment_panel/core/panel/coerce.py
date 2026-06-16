"""Generic numeric, text, symbol, and JSON coercion helpers."""

from __future__ import annotations
import json
import re
from typing import Any

# Pure numeric/stat/format helpers now live in core.coercion (the shared scalar
# leaf). Re-exported here under their legacy `_`-names so existing
# `core.panel.coerce` consumers keep working; new code should import them from
# core.coercion directly.
from investment_panel.core.coercion import average as _average
from investment_panel.core.coercion import decode_json_value
from investment_panel.core.coercion import format_metric as _format_metric
from investment_panel.core.coercion import iso_or_none as _iso_or_none
from investment_panel.core.coercion import median as _median
from investment_panel.core.coercion import number_from_any as _number_from_any
from investment_panel.core.coercion import optional_number as _optional_number
from investment_panel.core.coercion import share as _share
from investment_panel.core.coercion import string_list as _string_list


def _percentile_rank(values: list[float | None], current: float | None) -> float | None:
    cleaned = sorted(value for value in values if value is not None and value == value)
    if current is None or len(cleaned) < 2:
        return None
    below = sum(1 for value in cleaned if value < current)
    return round((below / (len(cleaned) - 1)) * 100, 2)




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




# Short, all-caps English words that collide with real tickers (e.g. LOW=Lowe's,
# ON=ON Semi, HIT, ALL=Allstate). A bare appearance in prose is almost always the
# English word, so we only accept these as tickers when they are cashtagged
# ($LOW) or carry an exchange prefix (NYSE:LOW).
_TICKER_WORD_STOPLIST = {
    "A", "ALL", "AN", "AND", "ANY", "ARE", "AS", "AT", "BE", "BIG", "BUT", "BY",
    "CAN", "DD", "DO", "FOR", "GO", "GOOD", "HAS", "HE", "HIT", "IF", "IN", "IS",
    "IT", "ITS", "KEY", "LOW", "NEW", "NO", "NOT", "NOW", "OF", "OFF", "ON", "ONE",
    "OR", "OUT", "OWN", "PAY", "REAL", "RUN", "SEE", "SO", "TO", "TOP", "TWO", "UP",
    "US", "WE", "WELL",
}


def _symbols_from_text(value: Any, known_symbols: set[str]) -> list[str]:
    text = str(value or "").upper()
    symbols = []
    for symbol in sorted(known_symbols, key=len, reverse=True):
        if not symbol or len(symbol) < 2:
            continue
        if not re.search(rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])", text):
            continue
        if symbol in _TICKER_WORD_STOPLIST and not re.search(
            rf"(?:\$|[A-Z]+:){re.escape(symbol)}(?![A-Z0-9])", text
        ):
            continue
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




def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or not denominator:
        return None
    return numerator / denominator




def _meaningful_text(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text in {"", "-", "none", "None", "null", "N/A", "n/a"} else text




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
