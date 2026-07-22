"""Cohort-accurate leg reconstruction for v3 paper-shadow marks."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def cohort_legs(connection: Any, generation_id: int, shadows: list[dict[str, Any]]) -> dict[Any, list[dict[str, Any]]]:
    contract_ids = sorted({int(leg["contract_id"]) for shadow in shadows for leg in shadow.get("synthetic_legs", [])})
    if not contract_ids:
        return {}
    quotes = {int(row["contract_id"]): dict(row) for row in connection.execute(
        """SELECT contract_id, bid, ask, bid_size, ask_size, provider_observed_at, available_at
           FROM raw.option_quote WHERE capture_generation_id = %s AND contract_id = ANY(%s)""",
        [generation_id, contract_ids],
    ).fetchall()}
    result: dict[Any, list[dict[str, Any]]] = {}
    for shadow in shadows:
        legs = []
        for stored in shadow.get("synthetic_legs", []):
            if (quote := quotes.get(int(stored["contract_id"]))) is not None:
                legs.append({**dict(stored), "bid": quote["bid"], "ask": quote["ask"], "observed_at": quote["provider_observed_at"],
                             "size_available": quote["bid_size"] is not None and quote["bid_size"] >= 1 and quote["ask_size"] is not None and quote["ask_size"] >= 1,
                             "available_at": quote["available_at"]})
        result[shadow["id"]] = legs
    return result


def latest_available_at(legs: list[dict[str, Any]]) -> datetime | None:
    values = [leg["available_at"] for leg in legs if leg.get("available_at") is not None]
    return max(values) if values else None
