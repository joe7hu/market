"""JSON-safe option-leg evidence summaries for QQQ underwriting."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def quote_package(legs: list[dict[str, Any]], as_of: datetime) -> dict[str, Any]:
    observed = [_as_datetime(leg.get("observed_at")) for leg in legs]
    timestamps = [value for value in observed if value is not None]
    ages = [(as_of - value).total_seconds() for value in timestamps]
    skew = (max(timestamps) - min(timestamps)).total_seconds() if len(timestamps) > 1 else 0.0
    return {
        "max_quote_age_seconds": max(ages) if ages else None,
        "interleg_skew_seconds": skew,
        "liquidity": {
            "minimum_open_interest": min((int(leg["open_interest"]) for leg in legs if leg.get("open_interest") is not None), default=None),
            "minimum_volume": min((int(leg["volume"]) for leg in legs if leg.get("volume") is not None), default=None),
            "displayed_sizes": [
                {"contract_id": leg["contract_id"], "bid_size": leg.get("bid_size"), "ask_size": leg.get("ask_size")}
                for leg in legs
            ],
        },
    }


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
