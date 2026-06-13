"""Candidate data-quality classification for options radar rows."""

from __future__ import annotations

from typing import Any

from investment_panel.core.options_radar_coerce import _elapsed_hours, _json, _number
from investment_panel.core.options_radar_constants import (
    OPTION_PEER_CROSSCHECK_MAX_AGE_HOURS,
    OPTION_QUALITY_DELTA_BAD_ABSOLUTE_DIFF,
    OPTION_QUALITY_DELTA_CAUTION_ABSOLUTE_DIFF,
    OPTION_QUALITY_IV_BAD_RELATIVE_DIFF,
    OPTION_QUALITY_IV_CAUTION_RELATIVE_DIFF,
    OPTION_QUALITY_MID_BAD_RELATIVE_DIFF,
    OPTION_QUALITY_MID_CAUTION_RELATIVE_DIFF,
)


def _candidate_quality(row: dict[str, Any], *, state: str, blockers: list[str], hard_rejects: list[str]) -> dict[str, Any]:
    if state == "REJECT":
        return {"status": "ok", "flags": [], "peer": {}}

    flags: list[str] = []
    bad_flags: set[str] = set()
    raw = _json(row.get("raw"))
    greeks_source = str(raw.get("greeks_source") or "provider")
    data_source = str(row.get("data_source") or "unknown")
    peer_source = row.get("peer_data_source")
    peer: dict[str, Any] = {"source": peer_source} if peer_source else {}
    peer_age_hours = _elapsed_hours(row.get("peer_snapshot_time"), row.get("snapshot_time"))
    peer_fresh = peer_age_hours is None or peer_age_hours <= OPTION_PEER_CROSSCHECK_MAX_AGE_HOURS
    if peer_source and peer_age_hours is not None:
        peer["age_hours"] = round(peer_age_hours, 2)
    if peer_source and not peer_fresh:
        peer["crosscheck_skipped"] = "stale_peer_snapshot"

    missing_flags = [blocker for blocker in blockers if blocker in {"missing_delta", "missing_spread", "missing_open_interest", "missing_volume", "missing_iv_percentile"}]
    if missing_flags:
        flags.extend(missing_flags)
        bad_flags.update(missing_flags)
    if "spread_above_fire_threshold" in blockers:
        flags.append("spread_above_threshold")
    if any(reject in hard_rejects for reject in {"spread_reject"}):
        flags.append("spread_reject")
        bad_flags.add("spread_reject")
    if state == "FIRE" and greeks_source in {"black_scholes_model", "mixed_fallback"}:
        flags.append("modeled_greeks")
    if state == "FIRE" and greeks_source == "mixed_fallback":
        flags.append("mixed_greek_sources")

    data_status = str(raw.get("market_data") or raw.get("market_data_type") or raw.get("data_status") or raw.get("entitlement_status") or "").lower()
    if "delayed" in data_status:
        flags.append("delayed_market_data")
    if "stale" in data_status:
        flags.append("stale_market_data")
        bad_flags.add("stale_market_data")

    mid = _number(row.get("mid"))
    peer_mid = _number(row.get("peer_mid"))
    mid_diff = _relative_diff(mid, peer_mid) if peer_fresh else None
    if mid_diff is not None:
        peer["mid_relative_diff"] = round(mid_diff, 4)
        if mid_diff >= OPTION_QUALITY_MID_BAD_RELATIVE_DIFF:
            flags.append("source_mid_disagreement")
            bad_flags.add("source_mid_disagreement")
        elif mid_diff >= OPTION_QUALITY_MID_CAUTION_RELATIVE_DIFF:
            flags.append("source_mid_disagreement")

    iv = _number(row.get("iv"))
    peer_iv = _number(row.get("peer_iv"))
    iv_diff = _relative_diff(iv, peer_iv) if peer_fresh else None
    if iv_diff is not None:
        peer["iv_relative_diff"] = round(iv_diff, 4)
        if iv_diff >= OPTION_QUALITY_IV_BAD_RELATIVE_DIFF:
            flags.append("source_iv_disagreement")
            bad_flags.add("source_iv_disagreement")
        elif iv_diff >= OPTION_QUALITY_IV_CAUTION_RELATIVE_DIFF:
            flags.append("source_iv_disagreement")

    delta = _number(row.get("delta"))
    peer_delta = _number(row.get("peer_delta"))
    if peer_fresh and delta is not None and peer_delta is not None:
        delta_diff = abs(delta - peer_delta)
        peer["delta_absolute_diff"] = round(delta_diff, 4)
        if delta_diff >= OPTION_QUALITY_DELTA_BAD_ABSOLUTE_DIFF:
            flags.append("source_delta_disagreement")
            bad_flags.add("source_delta_disagreement")
        elif delta_diff >= OPTION_QUALITY_DELTA_CAUTION_ABSOLUTE_DIFF:
            flags.append("source_delta_disagreement")

    deduped_flags = list(dict.fromkeys(flags))
    if bad_flags:
        status = "bad"
    elif deduped_flags:
        status = "caution"
    else:
        status = "ok"
    return {
        "status": status,
        "flags": deduped_flags,
        "source": data_source,
        "greeks_source": greeks_source,
        "peer": peer,
    }


def _relative_diff(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    denominator = max(abs(left), abs(right))
    if denominator <= 0:
        return None
    return abs(left - right) / denominator
