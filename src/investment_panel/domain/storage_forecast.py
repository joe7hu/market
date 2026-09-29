"""Conservative volume-growth forecasting from actual, distinct sample clocks."""

from __future__ import annotations

from datetime import UTC, date, datetime
from math import ceil, isfinite
from typing import Any, Mapping, Sequence

FALLBACK_BYTES_PER_DAY = 0.7 * 1024**3
MINIMUM_SPAN_SECONDS = 48 * 3600


def forecast_growth(
    samples: Sequence[Mapping[str, Any]], *, as_of: datetime | None = None
) -> dict[str, Any]:
    """Do not turn repeated checks or a midnight crossing into measured days.

    The maximum of volume consumption and database allocation protects against
    unrelated filesystem changes. A recent full-day growth burst cannot be
    cancelled by earlier reclamation in the endpoint average. Negative growth
    never earns a forecast credit or proves physical archive reclamation.
    """
    days: dict[date, tuple[datetime, dict[str, float]]] = {}
    for sample in samples:
        clock = sample.get("sampled_at")
        if (
            not isinstance(clock, datetime)
            or clock.tzinfo is None
            or clock.utcoffset() is None
        ):
            continue
        clock = clock.astimezone(UTC)
        if sample.get("sample_day") != clock.date() or (
            as_of is not None and clock > as_of
        ):
            continue
        try:
            values = {
                key: float(sample[key])
                for key in (
                    "database_bytes",
                    "volume_free_bytes",
                    "archived_bytes",
                    "logical_evidence_bytes",
                )
            }
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not all(isfinite(value) and value >= 0 for value in values.values()):
            continue
        if clock.date() not in days or days[clock.date()][0] < clock:
            days[clock.date()] = (clock, values)
    rows = sorted(days.values())
    span = (rows[-1][0] - rows[0][0]).total_seconds() if rows else 0
    recent_enough = as_of is None or bool(
        rows and (as_of - rows[-1][0]).total_seconds() <= MINIMUM_SPAN_SECONDS
    )
    measured = len(rows) >= 3 and span >= MINIMUM_SPAN_SECONDS and recent_enough
    rate = FALLBACK_BYTES_PER_DAY
    throughput = logical_rate = None
    if measured:
        first, last = rows[0], rows[-1]

        def growth(start: tuple[datetime, dict[str, float]]) -> float:
            elapsed_days = (last[0] - start[0]).total_seconds() / 86400
            return (
                max(
                    0.0,
                    start[1]["volume_free_bytes"] - last[1]["volume_free_bytes"],
                    last[1]["database_bytes"] - start[1]["database_bytes"],
                )
                / elapsed_days
            )

        # Avoid extrapolating a few minutes between changing daily sample slots.
        recent = next(
            row
            for row in reversed(rows[:-1])
            if (last[0] - row[0]).total_seconds() >= 20 * 3600
        )
        rate = max(growth(first), growth(recent))
        elapsed_days = span / 86400
        throughput = (
            max(0.0, last[1]["archived_bytes"] - first[1]["archived_bytes"])
            / elapsed_days
        )
        logical_rate = (
            last[1]["logical_evidence_bytes"] - first[1]["logical_evidence_bytes"]
        ) / elapsed_days
    return {
        "forecast_confidence": "measured" if measured else "provisional",
        "forecast_growth_bytes_per_day": ceil(rate),
        "measured_growth_bytes_per_day": ceil(rate) if measured else None,
        "forecast_growth_basis": "max_elapsed_endpoint_and_recent_growth"
        if measured
        else "fallback_0.7_GiB_per_day",
        "archive_throughput_bytes_per_day": None
        if throughput is None
        else int(throughput),
        "tracked_evidence_allocated_growth_bytes_per_day": None
        if logical_rate is None
        else int(logical_rate),
        "valid_sample_days": len(rows),
        "sample_span_seconds": span,
        "minimum_sample_days": 3,
        "minimum_sample_span_seconds": MINIMUM_SPAN_SECONDS,
        "latest_accounting_sample_at": rows[-1][0].isoformat() if rows else None,
    }
