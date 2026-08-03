"""Pure sell-off event detection and frozen contract-strip selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Iterable

from investment_panel.core.decision import MARKET_CLOSE, MARKET_OPEN, MARKET_TZ, is_us_market_day


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


@dataclass(frozen=True)
class FrozenContract:
    contract_key: str
    option_type: str
    expiration: date
    target_delta: float
    is_initial: bool
    retired_at: datetime | None = None


@dataclass(frozen=True)
class StripSelection:
    rows: tuple[dict[str, Any], ...]
    expected_contract_keys: tuple[str, ...]
    replacements: dict[str, str]


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


def event_severity(observation: EventObservation) -> float:
    """Rank simultaneous events by severity, liquidity, relevance, and evidence."""

    downside = max(
        0.0,
        -(observation.intraday_pct or 0.0),
        -(observation.one_day_pct or 0.0),
        -(observation.three_session_pct or 0.0),
    )
    return round(
        downside * 100.0
        + max(0.0, observation.liquidity_score) * 0.20
        + max(0.0, observation.relevance_score) * 2.0
        + max(0, observation.material_evidence_count) * 1.5,
        6,
    )


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
) -> StripSelection:
    """Freeze a calls+puts, 3-expiry delta ladder without erasing originals."""

    existing_rows = [contract for contract in existing if contract.retired_at is None]
    raw_rows = [dict(row) for row in rows]
    eligible = [row for row in raw_rows if _eligible_row(row, as_of)]
    expiration_dates = sorted({_expiration(row) for row in eligible})[:EVENT_MAX_EXPIRIES]
    eligible = [row for row in eligible if _expiration(row) in expiration_dates]
    by_key = {_contract_key(row): row for row in eligible}
    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    expected = {contract.contract_key for contract in existing_rows}
    replacements: dict[str, str] = {}

    # Retain exact original/replacement identities first whenever the provider
    # can still quote them.  Their absence remains visible through continuity.
    for contract in existing_rows:
        row = by_key.get(contract.contract_key)
        if row is not None:
            selected.append({**row, "_event_target_delta": contract.target_delta, "_event_initial": contract.is_initial})
            selected_keys.add(contract.contract_key)

    for expiration in expiration_dates:
        for option_type in ("call", "put"):
            ladder = [
                contract
                for contract in existing_rows
                if contract.expiration == expiration and contract.option_type == option_type
            ]
            for target in DELTA_LADDER:
                exact = next((contract for contract in ladder if abs(contract.target_delta - target) < 1e-9), None)
                if exact is not None and exact.contract_key in selected_keys:
                    continue
                candidates = [
                    row for row in eligible
                    if _expiration(row) == expiration
                    and _option_type(row) == option_type
                    and _contract_key(row) not in selected_keys
                    and _absolute_delta(row) is not None
                ]
                if not candidates:
                    continue
                selected_row = min(candidates, key=lambda row: abs((_absolute_delta(row) or 0.0) - target))
                key = _contract_key(selected_row)
                selected_keys.add(key)
                if exact is not None:
                    replacements[key] = exact.contract_key
                    expected.add(exact.contract_key)
                else:
                    expected.add(key)
                selected.append({
                    **selected_row,
                    "_event_target_delta": target,
                    "_event_initial": not existing_rows,
                    "_event_replaces_contract_key": replacements.get(key),
                })
    return StripSelection(
        rows=tuple(selected),
        expected_contract_keys=tuple(sorted(expected)),
        replacements=replacements,
    )


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
            local_slot = datetime.combine(cursor, time(MARKET_OPEN.hour, MARKET_OPEN.minute), MARKET_TZ)
            close_slot = datetime.combine(cursor, time(MARKET_CLOSE.hour, MARKET_CLOSE.minute), MARKET_TZ)
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
