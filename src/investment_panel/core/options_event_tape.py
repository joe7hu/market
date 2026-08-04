"""Pure sell-off event detection and frozen contract-strip selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable

from investment_panel.core.decision import MARKET_TZ, is_us_market_day, market_session_bounds


DELTA_LADDER = (0.25, 0.35, 0.45, 0.55, 0.65, 0.75)
EVENT_MIN_DTE = 7
EVENT_MAX_DTE = 45
EVENT_MAX_EXPIRIES = 3
EVENT_CAPTURE_MINUTES = 15
EVENT_MAX_ACTIVE_SYMBOLS = 2


@dataclass(frozen=True)
class EventObservation:
    symbol: str
    observed_at: datetime
    price: float
    one_day_pct: float | None = None
    intraday_pct: float | None = None
    three_session_pct: float | None = None
    reference_price: float | None = None
    liquidity_score: float = 0.0
    relevance_score: float = 0.0
    material_evidence_count: int = 0
    instrument_id: int | None = None
    source_id: str | None = None
    quote_available_at: datetime | None = None
    reference_trading_date: date | None = None
    reference_source_id: str | None = None
    reference_available_at: datetime | None = None
    quote_age_minutes: float | None = None
    data_quality_status: str = "valid"
    optionability_score: float | None = None
    owned: bool = False
    watched: bool = False
    recent_radar: bool = False


@dataclass(frozen=True)
class FrozenContract:
    contract_key: str
    option_type: str
    expiration: date
    target_delta: float
    is_initial: bool
    retired_at: datetime | None = None
    ladder_slot_key: str | None = None


@dataclass(frozen=True)
class StripSelection:
    rows: tuple[dict[str, Any], ...]
    expected_contract_keys: tuple[str, ...]
    expected_slot_keys: tuple[str, ...]
    replacements: dict[str, str]
    retire_contract_keys: tuple[str, ...] = ()


def trigger_reason(observation: EventObservation) -> str | None:
    """Return the strongest qualifying sell-off condition, if any."""

    moves = {
        "intraday_down_6pct": observation.intraday_pct,
        "one_day_down_6pct": observation.one_day_pct,
        "three_session_down_10pct": observation.three_session_pct,
    }
    qualifying = [
        reason
        for reason, value in moves.items()
        if value is not None and value <= (-0.10 if reason == "three_session_down_10pct" else -0.06)
    ]
    if not qualifying:
        return None
    return min(qualifying, key=lambda reason: moves[reason] if moves[reason] is not None else 0.0)


def event_priority_components(observation: EventObservation) -> dict[str, float]:
    """Bounded opportunity score with no raw quote-row-count contribution."""

    reason = trigger_reason(observation)
    selected_return = {
        "intraday_down_6pct": observation.intraday_pct,
        "one_day_down_6pct": observation.one_day_pct,
        "three_session_down_10pct": observation.three_session_pct,
    }.get(reason)
    downside = max(0.0, -(selected_return or 0.0))
    selloff = min(downside, 0.30) / 0.30 * 60.0
    # ``liquidity_score`` is retained only as a compatibility input for old
    # deterministic fixtures.  Live detection supplies the categorical
    # optionability score from distinct contract availability, never a row
    # count.
    optionability = observation.optionability_score
    if optionability is None:
        optionability = max(0.0, min(float(observation.liquidity_score), 25.0))
    optionability = min(max(float(optionability), 0.0), 25.0)
    material = min(max(int(observation.material_evidence_count), 0), 3) * 5.0
    return {
        "selloff_magnitude": round(selloff, 6),
        "optionability": round(optionability, 6),
        "material_evidence": round(material, 6),
        "total": round(selloff + optionability + material, 6),
    }


def event_severity(observation: EventObservation) -> float:
    """Return the bounded 100-point recovery opportunity score."""

    return event_priority_components(observation)["total"]


def event_reference_price(observation: EventObservation) -> float:
    if observation.reference_price is not None and observation.reference_price > 0:
        return observation.reference_price
    move = observation.one_day_pct if observation.one_day_pct is not None else observation.three_session_pct
    if move is None or move <= -0.99:
        return observation.price
    return observation.price / (1.0 + move)


def select_event_strip(
    rows: Iterable[dict[str, Any]],
    *,
    as_of: date,
    existing: Iterable[FrozenContract] = (),
    original_contract_keys: Iterable[str] = (),
    retired_contract_keys: Iterable[str] = (),
) -> StripSelection:
    """Select fixed logical ladder slots and at most one successor per slot.

    A slot is ``expiry-rank:type:target-delta``.  It remains one denominator
    member even when its quoted contract rolls; the original contract stays an
    immutable audit identity and its later absence remains visible separately.
    """

    existing_rows = [contract for contract in existing if contract.retired_at is None]
    active_contract_keys = {contract.contract_key for contract in existing_rows}
    raw_rows = [dict(row) for row in rows]
    retired_keys = {str(key) for key in retired_contract_keys if str(key)}
    eligible = [
        row for row in raw_rows
        if _eligible_row(row, as_of) and _contract_key(row) not in retired_keys
    ]
    expiration_dates = sorted({_expiration(row) for row in eligible})[:EVENT_MAX_EXPIRIES]
    eligible = [row for row in eligible if _expiration(row) in expiration_dates]
    by_key = {_contract_key(row): row for row in eligible}
    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    original = set(str(key) for key in original_contract_keys if str(key))
    original.update(contract.contract_key for contract in existing_rows if contract.is_initial)
    expected_slots = {
        contract.ladder_slot_key
        for contract in existing_rows if contract.ladder_slot_key
    }
    replacements: dict[str, str] = {}
    retired: list[str] = []

    # New events freeze their actual selected slots at inception.  Later calls
    # resolve solely from active logical slots and cannot add an expiry/strike
    # just because the provider returns more contracts.
    if not existing_rows:
        for expiry_rank, expiration in enumerate(expiration_dates):
            for option_type in ("call", "put"):
                for target in DELTA_LADDER:
                    candidates = [
                        row for row in eligible
                        if _expiration(row) == expiration and _option_type(row) == option_type
                        and _contract_key(row) not in selected_keys and _absolute_delta(row) is not None
                    ]
                    if not candidates:
                        continue
                    selected_row = min(candidates, key=lambda row: abs((_absolute_delta(row) or 0.0) - target))
                    key = _contract_key(selected_row)
                    slot = ladder_slot_key(expiry_rank, option_type, target)
                    selected_keys.add(key)
                    expected_slots.add(slot)
                    original.add(key)
                    selected.append({
                        **selected_row,
                        "_event_target_delta": target,
                        "_event_initial": True,
                        "_event_ladder_slot_key": slot,
                    })
        return StripSelection(
            rows=tuple(selected),
            expected_contract_keys=tuple(sorted(original)),
            expected_slot_keys=tuple(sorted(expected_slots)),
            replacements=replacements,
        )

    # The selector receives active members only.  Each active member maps to
    # exactly one slot and either survives unchanged or has one successor.
    for index, contract in enumerate(sorted(existing_rows, key=lambda item: (item.ladder_slot_key or "", item.contract_key))):
        slot = contract.ladder_slot_key or ladder_slot_key(
            _expiry_rank(contract.expiration, expiration_dates, fallback=index // (len(DELTA_LADDER) * 2)),
            contract.option_type,
            contract.target_delta,
        )
        expected_slots.add(slot)
        exact = by_key.get(contract.contract_key)
        if exact is not None and contract.contract_key not in selected_keys:
            selected_keys.add(contract.contract_key)
            selected.append({
                **exact,
                "_event_target_delta": contract.target_delta,
                "_event_initial": contract.is_initial,
                "_event_ladder_slot_key": slot,
            })
            continue
        candidates = [
            row for row in eligible
            if _option_type(row) == contract.option_type
            and _contract_key(row) not in selected_keys
            # A still-active member belongs to its own logical slot. A
            # missing neighbor may roll only into a new, unassigned contract;
            # it must never steal an active contract and cause a slot swap.
            and _contract_key(row) not in active_contract_keys
            and _absolute_delta(row) is not None
        ]
        # Keep replacements local to their existing expiration whenever the
        # provider still catalogs that expiry; only then fall back to the
        # nearest eligible expiry for a true successor transition.
        same_expiry = [row for row in candidates if _expiration(row) == contract.expiration]
        candidates = same_expiry or candidates
        if not candidates:
            continue
        selected_row = min(
            candidates,
            key=lambda row: (
                abs((_absolute_delta(row) or 0.0) - contract.target_delta),
                abs((_expiration(row) - contract.expiration).days),
                _contract_key(row),
            ),
        )
        key = _contract_key(selected_row)
        selected_keys.add(key)
        replacements[key] = contract.contract_key
        retired.append(contract.contract_key)
        selected.append({
            **selected_row,
            "_event_target_delta": contract.target_delta,
            "_event_initial": False,
            "_event_ladder_slot_key": slot,
            "_event_replaces_contract_key": contract.contract_key,
        })
    return StripSelection(
        rows=tuple(selected),
        expected_contract_keys=tuple(sorted(original)),
        expected_slot_keys=tuple(sorted(expected_slots)),
        replacements=replacements,
        retire_contract_keys=tuple(sorted(retired)),
    )


def ladder_slot_key(expiry_rank: int, option_type: str, target_delta: float) -> str:
    """Stable logical slot identity used by event-strip lineage."""

    return f"{max(0, int(expiry_rank))}:{str(option_type).lower()}:{float(target_delta):.2f}"


def scheduled_event_slots(start: datetime, end: datetime) -> list[datetime]:
    """Return scheduled 15-minute regular-session slots, inclusive of 16:00 ET."""

    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("event slot boundaries must be timezone-aware")
    if end < start:
        return []
    local_start = start.astimezone(MARKET_TZ)
    local_end = end.astimezone(MARKET_TZ)
    slots: list[datetime] = []
    cursor = local_start.date()
    while cursor <= local_end.date():
        if is_us_market_day(cursor):
            local_slot, close_slot = market_session_bounds(cursor)
            while local_slot <= close_slot:
                utc_slot = local_slot.astimezone(UTC)
                if start <= utc_slot <= end:
                    slots.append(utc_slot)
                local_slot += timedelta(minutes=EVENT_CAPTURE_MINUTES)
        cursor += timedelta(days=1)
    return slots


def trading_sessions_between(start: datetime, end: datetime) -> int:
    """Count calendar market sessions touched by a forward-only event."""

    if end < start:
        return 0
    first = start.astimezone(MARKET_TZ).date()
    last = end.astimezone(MARKET_TZ).date()
    return sum(is_us_market_day(first + timedelta(days=offset)) for offset in range((last - first).days + 1))


def event_strip_expiration_after_fill(
    fill_at: datetime,
    *,
    holding_sessions: int = 10,
) -> datetime:
    """Keep an event strip through the fill-relative holding horizon.

    The entry session is the zero point for lifecycle exits.  This returns a
    small grace period after the close of the tenth *subsequent* US trading
    session so the final scheduled capture can finish before the policy
    expires.  It intentionally uses the market calendar instead of calendar
    days, including holidays and early closes.
    """

    if fill_at.tzinfo is None:
        raise ValueError("fill timestamp must be timezone-aware")
    if holding_sessions < 1:
        raise ValueError("holding_sessions must be positive")
    trading_date = fill_at.astimezone(MARKET_TZ).date()
    elapsed = 0
    while elapsed < holding_sessions:
        trading_date += timedelta(days=1)
        if is_us_market_day(trading_date):
            elapsed += 1
    _open_at, close_at = market_session_bounds(trading_date)
    return close_at + timedelta(minutes=15)


def _eligible_row(row: dict[str, Any], as_of: date) -> bool:
    try:
        expiration = _expiration(row)
        return EVENT_MIN_DTE <= (expiration - as_of).days <= EVENT_MAX_DTE and _option_type(row) in {"call", "put"}
    except (TypeError, ValueError):
        return False


def _expiration(row: dict[str, Any]) -> date:
    value = row.get("expiration") or row.get("expiry")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _option_type(row: dict[str, Any]) -> str:
    return str(row.get("option_type") or row.get("type") or "").lower()


def _absolute_delta(row: dict[str, Any]) -> float | None:
    try:
        value = row.get("delta") if row.get("delta") is not None else row.get("provider_delta")
        return abs(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _contract_key(row: dict[str, Any]) -> str:
    key = row.get("contract_key") or row.get("contract_symbol") or row.get("robinhood_instrument_id") or row.get("contract_id")
    if key is None or not str(key).strip():
        raise ValueError("event strip row has no stable contract identity")
    return str(key)


def _expiry_rank(expiration: date, expirations: Iterable[date], *, fallback: int) -> int:
    ordered = sorted(set(expirations))
    try:
        return ordered.index(expiration)
    except ValueError:
        return max(0, int(fallback))
