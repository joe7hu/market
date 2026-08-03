"""Canonical v4 paper-only ticket contract for recovery opportunities."""

from __future__ import annotations

from datetime import UTC, date, datetime
from math import isfinite
from typing import Any, Iterable

from investment_panel.core.options_recovery import (
    FEE_PER_CONTRACT_LEG,
    OBJECTIVE_VERSION,
    ExecutableLeg,
    executable_entry_price,
)


RECOVERY_TICKET_VERSION = 4
RECOVERY_ENTRY_CAPTURE_TTL = 2
RECOVERY_HARD_LOSS_PCT = 0.50
RECOVERY_TRAILING_STOP_PCT = 0.35
RECOVERY_MAX_SESSIONS = 10
RECOVERY_MIN_DTE = 5


def occ_symbol(
    symbol: str,
    expiration: date | str,
    option_type: str,
    strike: float,
) -> str:
    """Build the canonical OCC identifier when a provider does not supply one."""

    expiry = expiration if isinstance(expiration, date) else date.fromisoformat(str(expiration)[:10])
    code = "C" if str(option_type).lower() == "call" else "P"
    return f"{symbol.upper()}{expiry:%y%m%d}{code}{int(round(float(strike) * 1000)):08d}"


def build_recovery_ticket_v4(
    *,
    decision_id: str,
    event_id: str,
    symbol: str,
    family: str,
    expiration: date | str,
    legs: Iterable[dict[str, Any]],
    quantity: int,
    invalidation: str,
    created_at: datetime | None = None,
    lower_confidence_expectancy: float | None = None,
    blockers: Iterable[str] = (),
) -> dict[str, Any]:
    """Build an immutable recovery ticket with executable entry and exit terms."""

    now = _aware(created_at) or datetime.now(UTC)
    normalized = [_ticket_leg(symbol, expiration, leg) for leg in legs]
    static_blockers = [str(item) for item in blockers if str(item)]
    entry: float | None
    try:
        entry = executable_entry_price(_executable_legs(normalized))
    except ValueError:
        entry = None
        static_blockers.append("executable_entry_quote_required")
    if quantity <= 0:
        static_blockers.append("positive_quantity_required")
    if not invalidation.strip():
        static_blockers.append("deterministic_invalidation_required")
    if not normalized:
        static_blockers.append("exact_contract_required")
    leg_count = len(normalized)
    one_unit_max_loss = _round_money((entry or 0.0) * 100.0 + FEE_PER_CONTRACT_LEG * leg_count * 2)
    total_risk = _round_money(one_unit_max_loss * max(quantity, 0))
    state = "READY" if not static_blockers and entry is not None and quantity > 0 else "WATCH"
    expiry_text = expiration.isoformat() if isinstance(expiration, date) else str(expiration)[:10]
    return {
        "ticket_version": RECOVERY_TICKET_VERSION,
        "objective_version": OBJECTIVE_VERSION,
        "decision_id": str(decision_id),
        "event_id": str(event_id),
        "symbol": symbol.upper(),
        "family": family,
        "state": state,
        "structure": "long_option" if leg_count == 1 else "debit_spread",
        "expiration": expiry_text,
        "legs": normalized,
        "entry": {
            "limit_price": entry,
            "basis": "first_post_publication_ask_or_debit_plus_10pct_spread_slippage",
            "ttl": {
                "basis": "scheduled_event_strip_captures",
                "capture_count": RECOVERY_ENTRY_CAPTURE_TTL,
            },
            "created_at": now.isoformat(),
        },
        "risk": {
            "one_unit_max_loss": one_unit_max_loss,
            "total_risk": total_risk,
            "per_trade_limit": 500.0,
            "aggregate_open_risk_limit": 2500.0,
            "per_symbol_open_risk_limit": 1000.0,
            "sleeve_capital": 25000.0,
            "fee_per_contract_leg_per_side": FEE_PER_CONTRACT_LEG,
        },
        "invalidation": invalidation,
        "exit_ladder": {
            "basis": "bid_side_executable_less_symmetric_slippage_and_fees",
            "targets": [
                {"multiple": 2.0, "quantity_fraction": 0.25},
                {"multiple": 3.0, "quantity_fraction": 0.50},
                {"multiple": 4.0, "quantity_fraction": 0.25},
            ],
            "single_contract": "exit_entire_position_at_3x",
            "trailing_stop_after_3x": RECOVERY_TRAILING_STOP_PCT,
            "hard_exit": {
                "premium_loss_fraction": RECOVERY_HARD_LOSS_PCT,
                "trading_sessions": RECOVERY_MAX_SESSIONS,
                "minimum_dte": RECOVERY_MIN_DTE,
                "invalidation": invalidation,
            },
        },
        "forecast": {
            "lower_confidence_executable_expectancy": _number(lower_confidence_expectancy),
        },
        "blockers": sorted(set(static_blockers)),
        "paper_only": True,
        "live_order_submission": False,
    }


def _ticket_leg(symbol: str, expiration: date | str, leg: dict[str, Any]) -> dict[str, Any]:
    option_type = str(leg.get("option_type") or "").lower()
    strike = _number(leg.get("strike"))
    if option_type not in {"call", "put"} or strike is None:
        raise ValueError("recovery ticket leg requires option type and strike")
    quote_time = _aware(leg.get("quote_time") or leg.get("available_at") or leg.get("observed_at"))
    return {
        # Match the shared public ticket-leg contract.  Persistence converts
        # this back to bigint only at the paper-order boundary.
        "contract_id": str(int(leg["contract_id"])),
        "occ_symbol": str(leg.get("occ_symbol") or occ_symbol(symbol, expiration, option_type, strike)),
        "option_type": option_type,
        "side": str(leg.get("side") or "buy"),
        "strike": strike,
        "bid": _number(leg.get("bid")),
        "ask": _number(leg.get("ask")),
        "bid_size": int(leg.get("bid_size") or 0),
        "ask_size": int(leg.get("ask_size") or 0),
        "quote_time": quote_time.isoformat() if quote_time else None,
        "open_interest": int(leg.get("open_interest") or 0),
        "volume": int(leg.get("volume") or 0),
    }


def _executable_legs(legs: list[dict[str, Any]]) -> list[ExecutableLeg]:
    return [
        ExecutableLeg(
            contract_id=str(leg["contract_id"]),
            side=str(leg["side"]),  # type: ignore[arg-type]
            bid=_number(leg.get("bid")),
            ask=_number(leg.get("ask")),
            bid_size=int(leg.get("bid_size") or 0),
            ask_size=int(leg.get("ask_size") or 0),
        )
        for leg in legs
    ]


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _number(value: Any) -> float | None:
    try:
        result = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return result if result is not None and isfinite(result) else None


def _round_money(value: float) -> float:
    return round(value + 1e-9, 2)
