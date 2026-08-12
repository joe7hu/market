"""Provider-neutral agent token-cost calculation."""

from __future__ import annotations

from typing import Any


def estimate_agent_cost(meta: dict[str, Any], pricing: dict[str, Any] | None) -> float:
    """Estimate a structured-agent call cost from reported token usage."""

    usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    table = pricing or {}
    model = str(meta.get("model") or "")
    rates = table.get(model) or table.get("default") or {}
    in_rate = float(rates.get("input_per_1m") or 0.0)
    out_rate = float(rates.get("output_per_1m") or 0.0)
    return round(input_tokens / 1_000_000 * in_rate + output_tokens / 1_000_000 * out_rate, 6)
