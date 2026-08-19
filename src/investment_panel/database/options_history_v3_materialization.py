"""Small deterministic helpers for option-history v3 materialization."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from math import isfinite
from typing import Any

from investment_panel.analysis.history_v3 import MIN_ELIGIBLE_POINTS, eligible_rows
from investment_panel.database.options_history_v3_surface import nearest_delta_iv


def group_verified_contract_rows(
    rows: list[dict[str, Any]],
) -> tuple[dict[tuple[Any, str], list[dict[str, Any]]], int, int]:
    """Keep one verified standard deliverable per expiry/type scope."""

    raw_grouped: dict[
        tuple[Any, str], dict[tuple[Any, str, str, str], list[dict[str, Any]]]
    ] = {}
    excluded_rows = 0
    for row in rows:
        terms = (
            row.get("multiplier"), str(row.get("style") or ""),
            str(row.get("settlement") or ""), str(row.get("deliverable_key") or ""),
        )
        if row.get("standard_contract_verified") is not True or any(
            value in {None, ""} for value in terms
        ):
            excluded_rows += 1
            continue
        scope = (row["expiration"], str(row["option_type"]))
        raw_grouped.setdefault(scope, {}).setdefault(terms, []).append(row)
    grouped: dict[tuple[Any, str], list[dict[str, Any]]] = {}
    ambiguous_scopes = 0
    for scope, term_groups in raw_grouped.items():
        if len(term_groups) != 1:
            ambiguous_scopes += 1
            excluded_rows += sum(len(group) for group in term_groups.values())
            continue
        grouped[scope] = next(iter(term_groups.values()))
    return grouped, ambiguous_scopes, excluded_rows


def capture_group_quality(rows: list[dict[str, Any]]) -> tuple[float | None, list[str]]:
    """Return capture-wide rejection reasons and one coherent spot."""

    if not rows:
        return None, ["empty_capture_group"]
    started = min(
        (row.get("group_started_at") for row in rows if row.get("group_started_at")),
        default=None,
    )
    finished = max(
        (row.get("group_finished_at") for row in rows if row.get("group_finished_at")),
        default=None,
    )
    blockers: list[str] = []
    if started is None or finished is None:
        blockers.append("missing_group_timestamps")
    elif (finished - started).total_seconds() > 60:
        blockers.append("group_duration_stale")
    values = [float(row["underlying_price"]) for row in rows if row.get("underlying_price") is not None]
    observed_at = [row.get("underlying_observed_at") for row in rows]
    if len(values) != len(rows) or any(value is None for value in observed_at):
        blockers.append("missing_aligned_underlying")
    elif len({round(value, 8) for value in values}) != 1 or len(set(observed_at)) != 1:
        blockers.append("inconsistent_aligned_underlying")
    return (values[0] if not blockers and values else None), sorted(set(blockers))


def is_later_capture_cohort(
    pending_slot: datetime | None, current_slot: datetime | None,
) -> bool:
    """True only when a candidate can enter after its own evidence cohort."""

    return pending_slot is not None and current_slot is not None and current_slot > pending_slot


def surface_summary(
    rows: list[dict[str, Any]], result: dict[str, Any], spot: float | None,
) -> dict[str, Any]:
    nearest = min(rows, key=lambda row: abs(float(row["strike"]) - (spot or float(row["strike"]))))
    quality_rows, _blockers, _metrics = eligible_rows(
        rows, spot=spot, option_type=str(nearest.get("option_type") or ""),
    )
    quality_nearest = (
        min(quality_rows, key=lambda row: abs(float(row["strike"]) - float(spot)))
        if spot is not None and len(quality_rows) >= MIN_ELIGIBLE_POINTS
        else None
    )
    atm_iv = _positive_finite(quality_nearest.get("provider_iv")) if quality_nearest else None
    spreads = [
        float(row["ask"] - row["bid"]) / float(row["mid"])
        for row in rows
        if row.get("mid") and row.get("ask") is not None and row.get("bid") is not None
    ]
    started = min(
        (row.get("group_started_at") for row in rows if row.get("group_started_at")),
        default=None,
    )
    finished = max(
        (row.get("group_finished_at") for row in rows if row.get("group_finished_at")),
        default=None,
    )
    ages = [
        (finished - row["provider_observed_at"]).total_seconds()
        for row in rows
        if finished and row.get("provider_observed_at")
    ]
    candidate_count = sum(value["classification"] != "rejected" for value in result["relative_values"])
    return {
        "dte": int(nearest["dte"]),
        "atm_iv": atm_iv,
        "delta_25_iv": nearest_delta_iv(quality_rows) if quality_nearest else None,
        "average_spread_pct": sum(spreads) / len(spreads) if spreads else None,
        "liquidity_score": float(len(rows)),
        "group_duration_seconds": (finished - started).total_seconds() if started and finished else None,
        "max_quote_age_seconds": max(ages) if ages else None,
        "candidate_count": candidate_count,
        "metrics": {
            "blockers": result["blockers"],
            "static_arbitrage": result["static_findings"],
            "fit": result["fit"].diagnostics,
            "row_metrics": result["row_metrics"],
        },
    }


def deterministic_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _positive_finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0 else None


def long_delta_eligible(quote: dict[str, Any]) -> bool:
    """Require a fresh provider delta in the release-1 long-option band."""

    try:
        delta = abs(float(quote.get("provider_delta")))
    except (TypeError, ValueError):
        return False
    observed = quote.get("provider_observed_at")
    available = quote.get("available_at")
    return (
        0.35 <= delta <= 0.65
        and observed is not None
        and available is not None
        and (available - observed).total_seconds() <= 180
    )


def policy_for_instrument(connection: Any, instrument_id: int) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT publication_cap, effective_state, policy_revision, lock_version
        FROM app.option_history_policy
        WHERE instrument_id = %s AND profile = 'history_full'
        """,
        [instrument_id],
    ).fetchone()
    return dict(row) if row else None
