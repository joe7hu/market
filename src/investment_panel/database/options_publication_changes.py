"""Point-in-time candidate-set change labels for options publications."""

from __future__ import annotations

from typing import Any


def candidate_changes(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
) -> dict[str, list[str]]:
    current_symbols = {str(row.get("ticker") or row.get("symbol") or "") for row in current}
    previous_symbols = {str(row.get("ticker") or row.get("symbol") or "") for row in previous}
    current_symbols.discard("")
    previous_symbols.discard("")
    return {
        "new": sorted(current_symbols - previous_symbols),
        "retained": sorted(current_symbols & previous_symbols),
        "removed": sorted(previous_symbols - current_symbols),
    }
