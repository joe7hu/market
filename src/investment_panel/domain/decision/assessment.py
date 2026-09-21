"""Quote admissibility for analysis, never permission to fill an order.

A data timestamp is evidence, not a UI refresh time. Equities retain the last
completed closing reference while closed; continuous markets consume wall time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any, Mapping

from investment_panel.domain.decision.calendar import (
    is_market_open, latest_completed_market_day, market_session_bounds, normalized_utc,
)

ASSESSMENT_QUOTE_SECONDS = 900


def timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return normalized_utc(value) if value.tzinfo is not None else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return normalized_utc(parsed) if parsed.tzinfo is not None else None
        except ValueError:
            pass
    return None


@dataclass(frozen=True)
class AssessmentQuote:
    usable: bool
    state: str
    reason: str
    price: float | None
    observed_at: datetime | None
    available_at: datetime | None
    next_session_at: datetime | None = None


def assessment_quote(
    row: Mapping[str, Any], *, now: datetime, continuous: bool = False,
) -> AssessmentQuote:
    reference = normalized_utc(now)
    observed = timestamp(row.get("observed_at"))
    available = timestamp(row.get("available_at"))
    try:
        if isinstance(row.get("price", row.get("close")), bool):
            raise ValueError("Boolean price")
        price = float(row.get("price", row.get("close")))
    except (TypeError, ValueError):
        price = None
    def result(usable: bool, state: str, reason: str) -> AssessmentQuote:
        return AssessmentQuote(usable, state, reason, price, observed, available)
    if price is None or not isfinite(price) or price <= 0:
        return result(False, "failed", "No positive, finite confirmed price")
    if observed is None or available is None:
        return result(False, "failed", "Provider observation or confirmation timestamp absent")
    if not observed <= available <= reference:
        return result(False, "failed", "Quote timestamps are not causal")
    budget = timedelta(seconds=ASSESSMENT_QUOTE_SECONDS)
    if continuous:
        return result(reference - observed <= budget, "live" if reference - observed <= budget else "overdue",
                      "24/7 quote" if reference - observed <= budget else "24/7 quote collector overdue")
    if is_market_open(reference):
        return result(reference - observed <= budget, "live" if reference - observed <= budget else "overdue",
                      "Regular-session reference" if reference - observed <= budget else "Regular-session quote collector overdue")
    _, latest_close = market_session_bounds(latest_completed_market_day(reference))
    # Last trade can precede the official close slightly; a missed session or
    # a Friday-morning quote must not become a valid Sunday closing reference.
    usable = observed >= latest_close.astimezone(UTC) - budget
    return result(usable, "closed_reference" if usable else "overdue",
                  "Last completed session; execution waits for an open venue" if usable
                  else "Latest completed-session closing reference was not collected")


__all__ = ["AssessmentQuote", "assessment_quote", "timestamp"]
