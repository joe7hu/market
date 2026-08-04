"""Small pure/transaction helpers for the recovery execution repository."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from investment_panel.core.options_recovery_registry import (
    RankedRecoveryCandidate,
    RecoveryContractQuote,
    RecoveryEventState,
)
from investment_panel.core.options_recovery_ticket import build_recovery_ticket_v4, occ_symbol
from investment_panel.core.options_recovery_paper import RecoveryRiskPolicy


def contract_quote(source: dict[str, Any]) -> RecoveryContractQuote | None:
    try:
        provider_symbols = dict(source.get("provider_symbols") or {})
        option_type = str(source["option_type"]).lower()
        strike = float(source["strike"])
        expiry = source["expiration"]
        stable = bool(source.get("contract_id")) and bool(source.get("event_contract_id"))
        return RecoveryContractQuote(
            contract_id=int(source["contract_id"]),
            occ_symbol=str(
                provider_symbols.get("occ")
                or provider_symbols.get("occ_symbol")
                or occ_symbol(str(source.get("symbol") or ""), expiry, option_type, strike)
            ),
            option_type=option_type,  # type: ignore[arg-type]
            expiration=expiry,
            strike=strike,
            bid=number(source.get("bid")),
            ask=number(source.get("ask")),
            bid_size=integer(source.get("bid_size")),
            ask_size=integer(source.get("ask_size")),
            open_interest=integer(source.get("open_interest")),
            delta=number(source.get("provider_delta")),
            observed_at=source["observed_at"],
            available_at=source["available_at"],
            stable_identity=stable,
            volume=integer(source.get("volume")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def one_unit_ticket(
    *,
    event: RecoveryEventState,
    family: str,
    quote: RecoveryContractQuote,
    decision_id: str,
    created_at: datetime,
    quantity: int = 1,
    blockers: Iterable[str] = (),
    lower_confidence_expectancy: float | None = None,
    risk_policy: RecoveryRiskPolicy | None = None,
) -> dict[str, Any]:
    invalidation = (
        "spot returns to or below the event low"
        if family.endswith("call_v1")
        else "spot reclaims half of the opening/event gap"
    )
    return build_recovery_ticket_v4(
        decision_id=decision_id,
        event_id=event.event_id,
        symbol=event.symbol,
        family=family,
        expiration=quote.expiration,
        quantity=quantity,
        invalidation=invalidation,
        created_at=created_at,
        lower_confidence_expectancy=lower_confidence_expectancy,
        blockers=blockers,
        risk_policy=risk_policy,
        legs=[{
            "contract_id": quote.contract_id,
            "occ_symbol": quote.occ_symbol,
            "option_type": quote.option_type,
            "side": "buy",
            "strike": quote.strike,
            "bid": quote.bid,
            "ask": quote.ask,
            "bid_size": quote.bid_size,
            "ask_size": quote.ask_size,
            "quote_time": quote.available_at,
            "open_interest": quote.open_interest,
            "volume": quote.volume,
        }],
    )


def select_published(
    candidates: dict[str, list[tuple[RankedRecoveryCandidate, dict[str, Any]]]],
) -> list[tuple[RankedRecoveryCandidate, dict[str, Any]]]:
    """Keep one eligible representative per family before filling two slots."""

    selected: list[tuple[RankedRecoveryCandidate, dict[str, Any]]] = []
    for family in sorted(candidates):
        if candidates[family]:
            selected.append(candidates[family][0])
    remaining = [item for rows in candidates.values() for item in rows[1:]]
    for item in sorted(remaining, key=lambda candidate: candidate[0].selection_score, reverse=True):
        if len(selected) >= 2:
            break
        selected.append(item)
    return selected[:2]


def selection_inputs(selected: Iterable[tuple[RankedRecoveryCandidate, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [
        {"family": candidate.family, "contract_id": candidate.quote.contract_id, "score": candidate.selection_score}
        for candidate, _source in selected
    ]


def decision_key(event_id: str, capture_id: str, candidate: RankedRecoveryCandidate) -> str:
    return f"recovery:{event_id}:{capture_id}:{candidate.family}:{candidate.quote.contract_id}"


def midpoint(bid: float | None, ask: float | None) -> float | None:
    return (bid + ask) / 2.0 if bid is not None and ask is not None else None


def journal(
    connection: Any,
    source: dict[str, Any],
    *,
    action: str,
    quantity: int,
    price: float | None,
    key: str,
    details: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO app.trade_journal (decision_id, instrument_id, action, quantity, price, rationale, details)
        SELECT %s, %s, %s, %s, %s, %s, %s
        WHERE NOT EXISTS (
          SELECT 1 FROM app.trade_journal WHERE details->>'idempotency_key' = %s
        )
        """,
        [
            source["decision_id"], source["instrument_id"], action, quantity, price,
            "options_recovery_deterministic_paper_lifecycle",
            Jsonb({
                "idempotency_key": key,
                "event_id": str(source.get("event_id") or ""),
                "cohort_id": str(source.get("cohort_id") or ""),
                "family": str(source.get("strategy_key") or source.get("strategy_family") or ""),
                "objective_version": "short_horizon_convex_v2",
                **details,
            }), key,
        ],
    )


def order_status(result: Any, quantity: int) -> str:
    if result.classification == "unfilled":
        return "unfilled"
    if result.classification == "unmeasurable":
        return "unmeasurable"
    exited = sum(fill.quantity for fill in result.exit_fills)
    if exited >= quantity:
        return "invalidated" if any(fill.reason == "invalidation" for fill in result.exit_fills) else "exited"
    if exited:
        return "partial_exited"
    return "entered" if result.entry_fill_at is not None else "staged"


def invalidated(family: str, price: float | None, event_low: float, reference_price: float) -> bool:
    if price is None:
        return False
    if family.endswith("call_v1"):
        return float(price) <= float(event_low)
    half_reclaim = float(event_low) + (float(reference_price) - float(event_low)) * 0.5
    return float(price) >= half_reclaim


def leg_expiry_days(leg: Any, observed_at: datetime) -> int:
    expiry = leg.get("expiration") if isinstance(leg, dict) else leg["expiration"]
    return (expiry - observed_at.date()).days


def utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def integer(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
