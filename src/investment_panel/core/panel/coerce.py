"""Symbol coercion helpers for panel payloads."""

from __future__ import annotations

from typing import Any

from investment_panel.core.coercion import string_list as _string_list


def _symbols_from_value(value: Any) -> list[str]:
    symbols = []
    for item in _string_list(value):
        symbol = _normalize_symbol_token(item)
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


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


symbols_from_value = _symbols_from_value


__all__ = ["symbols_from_value"]
