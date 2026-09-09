"""Stable, lane-local identities and trust rules for option opportunities.

An episode is a market hypothesis, not a quote capture or contract row.  The
same event can surface through many contracts and repeated capture loops, but
it is still one independent opportunity for scorecard purposes.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from zoneinfo import ZoneInfo


MARKET_TZ = ZoneInfo("America/New_York")

# The columns landed in migration 20260812_0027.  This marker lets readers
# keep pre-contract rows available for audit while keeping them out of the
# rebuilt scorecard denominator without a destructive rewrite.
SCORECARD_TRUTH_VERSION = "option-scorecard-truth-v1"
SCORECARD_TRUTH_PREFIX = f"{SCORECARD_TRUTH_VERSION}:"
SUPPORTED_OPTION_LANES = frozenset({"radar", "qqq", "recovery"})
TRUSTED_QUALITY_STATUSES = frozenset({"complete", "ok", "verified", "eligible"})
UNSAFE_QUALITY_STATUSES = frozenset({
    "invalid", "lookahead_blocked", "continuity_missing", "unverified",
    "legacy", "legacy_non_executable",
})


def canonical_option_lane(lane: str | None, *, symbol: str | None = None) -> str:
    """Return the only scorecard lane names accepted by option writers."""

    normalized = str(lane or "").strip().lower()
    if not normalized:
        normalized = "qqq" if str(symbol or "").strip().upper() == "QQQ" else "radar"
    if normalized not in SUPPORTED_OPTION_LANES:
        raise ValueError("option lane must be radar, qqq, or recovery")
    return normalized


def scorecard_truth_cohort(cohort: str | None = None) -> str:
    """Return a versioned cohort marker for new, scorecard-safe writes."""

    suffix = str(cohort or "default").strip() or "default"
    return f"{SCORECARD_TRUTH_PREFIX}{suffix}"


def has_current_scorecard_truth(cohort: object) -> bool:
    return str(cohort or "").startswith(SCORECARD_TRUTH_PREFIX)


def option_sample_eligibility(quality_status: str | None) -> tuple[str, bool, str | None]:
    """Normalize quality metadata and return the scorecard eligibility gate.

    Unknown quality is intentionally not trusted.  This prevents a newly added
    writer from silently contributing to calibration before it declares its
    data-quality contract.
    """

    normalized = str(quality_status or "").strip().lower()
    if not normalized:
        return "unverified", False, "quality_status_missing"
    if normalized in TRUSTED_QUALITY_STATUSES:
        return normalized, True, None
    if normalized in UNSAFE_QUALITY_STATUSES:
        return normalized, False, normalized
    return normalized, False, f"quality_status_{normalized}"


def option_episode_key(
    *,
    lane: str,
    symbol: str,
    strategy: str,
    contract_ladder_slot: str,
    entry_at: datetime,
    event_id: str | None = None,
) -> str:
    """Return one reproducible key for a lane-local market hypothesis.

    ``contract_ladder_slot`` remains in the signature so older writers stay
    source compatible, but is deliberately not part of the identity.  It is
    execution implementation detail, whereas the event/symbol/strategy and
    local trading date identify the independent hypothesis.  The parameter is
    therefore retained only as auditable caller metadata.
    """

    if entry_at.tzinfo is None:
        raise ValueError("episode entry time must be timezone-aware")
    local = entry_at.astimezone(MARKET_TZ)
    payload = {
        "lane": canonical_option_lane(lane, symbol=symbol),
        "event_id": str(event_id or ""),
        "symbol": str(symbol).strip().upper(),
        "strategy": str(strategy).strip(),
        "trading_date": local.date().isoformat(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:24]
    return f"{payload['lane']}:{digest}"


def normalized_entry_at(value: datetime) -> datetime:
    """Return a UTC timestamp for callers that accept external timestamps."""

    if value.tzinfo is None:
        raise ValueError("entry time must be timezone-aware")
    return value.astimezone(UTC)
