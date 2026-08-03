"""Executable, forward-only lifecycle math for short-horizon option tickets.

The recovery program deliberately keeps this logic free of database and provider
code.  Capture, selection, paper-order persistence, and learning all call the
same executable-side model, so a midpoint or a future quote cannot quietly
become a paper result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Iterable, Literal


OBJECTIVE_VERSION = "short_horizon_convex_v1"
FEE_PER_CONTRACT_LEG = 0.65
MIN_SLIPPAGE = 0.01
SPREAD_SLIPPAGE_FRACTION = 0.10
MAX_ENTRY_CAPTURES = 2
HARD_LOSS_MULTIPLE = 0.50
TRAILING_STOP_FRACTION = 0.35
MAX_TRADING_SESSIONS = 10
MIN_EXIT_DTE = 5

OutcomeClassification = Literal["captured", "missed", "unfilled", "unmeasurable", "observing"]


@dataclass(frozen=True)
class ExecutableLeg:
    """A single quoted leg; ``side`` is the entry side for the package."""

    contract_id: str
    side: Literal["buy", "sell", "long", "short"]
    bid: float | None
    ask: float | None
    bid_size: int | None = None
    ask_size: int | None = None

    @property
    def is_long(self) -> bool:
        return self.side in {"buy", "long"}

    @property
    def is_quoted(self) -> bool:
        return (
            self.bid is not None
            and self.ask is not None
            and self.bid > 0
            and self.ask >= self.bid
            and (self.bid_size is None or self.bid_size > 0)
            and (self.ask_size is None or self.ask_size > 0)
        )


@dataclass(frozen=True)
class QuoteCapture:
    """One scheduled same-contract capture after a ticket was published."""

    observed_at: datetime
    legs: tuple[ExecutableLeg, ...]
    session_number: int
    dte: int | None
    invalidated: bool = False
    continuity_ok: bool = True
    scheduled: bool = True
    reason: str | None = None


@dataclass(frozen=True)
class ExitFill:
    observed_at: datetime
    quantity: int
    executable_price: float
    reason: str
    session_number: int


@dataclass
class LifecycleResult:
    classification: OutcomeClassification = "observing"
    entry_fill_at: datetime | None = None
    entry_fill_price: float | None = None
    entry_fee: float = 0.0
    exit_fills: list[ExitFill] = field(default_factory=list)
    exit_fee: float = 0.0
    mfe: float | None = None
    mae: float | None = None
    executable_peak_return: float | None = None
    giveback: float | None = None
    time_to_2x_sessions: int | None = None
    time_to_3x_sessions: int | None = None
    time_to_4x_sessions: int | None = None
    unmeasurable_reason: str | None = None
    entry_capture_count: int = 0

    @property
    def filled_quantity(self) -> int:
        return sum(fill.quantity for fill in self.exit_fills)


def leg_slippage(leg: ExecutableLeg) -> float:
    """One-sided deterministic slippage for one leg, quoted in premium dollars."""

    if not leg.is_quoted:
        raise ValueError(f"executable quote required for {leg.contract_id}")
    assert leg.ask is not None and leg.bid is not None
    return max(MIN_SLIPPAGE, (leg.ask - leg.bid) * SPREAD_SLIPPAGE_FRACTION)


def executable_entry_price(legs: Iterable[ExecutableLeg]) -> float:
    """Debit paid at ask/bid sides, plus per-leg slippage.

    Long legs pay ask plus slippage; short legs receive bid minus slippage.
    The result is a positive debit only.  Credit structures are outside this
    short-horizon convex objective and therefore fail closed here.
    """

    quoted = tuple(legs)
    if not quoted:
        raise ValueError("at least one leg is required")
    price = sum(
        ((leg.ask or 0.0) + leg_slippage(leg)) if leg.is_long
        else -((leg.bid or 0.0) - leg_slippage(leg))
        for leg in quoted
    )
    if price <= 0:
        raise ValueError("short_horizon_convex_v1 requires a positive debit")
    return round(price, 6)


def executable_exit_price(legs: Iterable[ExecutableLeg]) -> float:
    """Close at bid/ask sides with symmetric per-leg slippage."""

    quoted = tuple(legs)
    if not quoted:
        raise ValueError("at least one leg is required")
    price = sum(
        ((leg.bid or 0.0) - leg_slippage(leg)) if leg.is_long
        else -((leg.ask or 0.0) + leg_slippage(leg))
        for leg in quoted
    )
    return round(max(price, 0.0), 6)


def staged_exit_quantities(quantity: int) -> tuple[int, int, int]:
    """Allocate 25% / 50% / 25% with deterministic largest remainder.

    A single contract cannot be meaningfully staged, so it exits entirely at
    3x.  Ties in fractional remainders resolve toward the earlier target.
    """

    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if quantity == 1:
        return (0, 1, 0)
    weights = (0.25, 0.50, 0.25)
    floors = [int(quantity * weight) for weight in weights]
    remainder = quantity - sum(floors)
    fractions = sorted(
        ((quantity * weight - floor, -index) for index, (weight, floor) in enumerate(zip(weights, floors))),
        reverse=True,
    )
    for _, neg_index in fractions[:remainder]:
        floors[-neg_index] += 1
    return tuple(floors)  # type: ignore[return-value]


def lifecycle_return(
    *,
    entry_price: float,
    exits: Iterable[ExitFill],
    quantity: int,
    leg_count: int,
    multiplier: int = 100,
) -> float | None:
    """Cost-adjusted return for a closed position using executable fills."""

    fills = tuple(exits)
    if quantity <= 0 or leg_count <= 0 or sum(fill.quantity for fill in fills) != quantity:
        return None
    entry_cash = entry_price * multiplier * quantity + FEE_PER_CONTRACT_LEG * leg_count * quantity
    if entry_cash <= 0:
        return None
    exit_cash = sum(fill.executable_price * multiplier * fill.quantity for fill in fills)
    exit_cash -= FEE_PER_CONTRACT_LEG * leg_count * quantity
    return (exit_cash - entry_cash) / entry_cash


def evaluate_lifecycle(
    *,
    published_at: datetime,
    quantity: int,
    captures: Iterable[QuoteCapture],
    entry_limit: float | None = None,
    multiplier: int = 100,
) -> LifecycleResult:
    """Evaluate a forward-only ticket against ordered scheduled captures.

    Entry is the first valid quote available *after* publication.  If the
    first two scheduled opportunities cannot produce an executable fill at the
    limit, the result is ``unfilled``.  Missing future same-contract evidence
    is ``unmeasurable`` and never becomes a synthetic loss or a missed winner.
    """

    if published_at.tzinfo is None:
        raise ValueError("publication timestamp must be timezone-aware")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    result = LifecycleResult()
    ordered = sorted(captures, key=lambda capture: capture.observed_at)
    entry_price: float | None = None
    legs_count = 0
    remaining = quantity
    target_quantities = staged_exit_quantities(quantity)
    reached_3x = False
    high_water_price: float | None = None
    peak_return: float | None = None

    for capture in ordered:
        if capture.observed_at.tzinfo is None:
            raise ValueError("capture timestamp must be timezone-aware")
        if capture.observed_at <= published_at:
            continue
        if not capture.continuity_ok:
            result.classification = "unmeasurable"
            result.unmeasurable_reason = capture.reason or "same_contract_continuity_missing"
            return result
        if not capture.legs:
            if result.entry_fill_at is None and capture.scheduled:
                result.entry_capture_count += 1
                if result.entry_capture_count >= MAX_ENTRY_CAPTURES:
                    result.classification = "unfilled"
                    return result
            continue
        if result.entry_fill_at is None:
            if capture.scheduled:
                result.entry_capture_count += 1
            try:
                candidate_entry = executable_entry_price(capture.legs)
            except ValueError:
                candidate_entry = None
            if candidate_entry is not None and (entry_limit is None or candidate_entry <= entry_limit):
                result.entry_fill_at = capture.observed_at
                result.entry_fill_price = candidate_entry
                result.entry_fee = FEE_PER_CONTRACT_LEG * len(capture.legs) * quantity
                entry_price = candidate_entry
                legs_count = len(capture.legs)
                continue
            if result.entry_capture_count >= MAX_ENTRY_CAPTURES:
                result.classification = "unfilled"
                return result
            continue

        assert entry_price is not None
        try:
            mark = executable_exit_price(capture.legs)
        except ValueError:
            result.classification = "unmeasurable"
            result.unmeasurable_reason = capture.reason or "executable_exit_quote_missing"
            return result
        package_return = _mark_return(entry_price, mark, len(capture.legs), multiplier)
        result.mfe = package_return if result.mfe is None else max(result.mfe, package_return)
        result.mae = package_return if result.mae is None else min(result.mae, package_return)
        result.executable_peak_return = result.mfe
        gross_multiple = mark / entry_price if entry_price > 0 else 0.0
        if gross_multiple >= 2.0 and result.time_to_2x_sessions is None:
            result.time_to_2x_sessions = capture.session_number
        if gross_multiple >= 3.0 and result.time_to_3x_sessions is None:
            result.time_to_3x_sessions = capture.session_number
        if gross_multiple >= 4.0 and result.time_to_4x_sessions is None:
            result.time_to_4x_sessions = capture.session_number
        high_water_price = mark if high_water_price is None else max(high_water_price, mark)

        hard_exit = (
            capture.invalidated
            or gross_multiple <= HARD_LOSS_MULTIPLE
            or capture.session_number >= MAX_TRADING_SESSIONS
            or (capture.dte is not None and capture.dte <= MIN_EXIT_DTE)
        )
        if hard_exit:
            _append_exit(result, capture, remaining, mark, "invalidation" if capture.invalidated else (
                "hard_loss" if gross_multiple <= HARD_LOSS_MULTIPLE else "time_or_dte_exit"
            ))
            remaining = 0
            break

        if gross_multiple >= 2.0 and target_quantities[0] and remaining:
            exit_quantity = min(target_quantities[0], remaining)
            _append_exit(result, capture, exit_quantity, mark, "target_2x")
            remaining -= exit_quantity
            target_quantities = (0, target_quantities[1], target_quantities[2])
        if gross_multiple >= 3.0 and target_quantities[1] and remaining:
            exit_quantity = min(target_quantities[1], remaining)
            _append_exit(result, capture, exit_quantity, mark, "target_3x")
            remaining -= exit_quantity
            target_quantities = (target_quantities[0], 0, target_quantities[2])
            reached_3x = True
        if gross_multiple >= 4.0 and target_quantities[2] and remaining:
            exit_quantity = min(target_quantities[2], remaining)
            _append_exit(result, capture, exit_quantity, mark, "target_4x")
            remaining -= exit_quantity
            target_quantities = (target_quantities[0], target_quantities[1], 0)
        if reached_3x and remaining and high_water_price and mark <= high_water_price * (1.0 - TRAILING_STOP_FRACTION):
            _append_exit(result, capture, remaining, mark, "trailing_stop_after_3x")
            remaining = 0
        if remaining == 0:
            break

    if result.entry_fill_at is None:
        # Fewer than two scheduled captures means evidence is incomplete, not a loss.
        result.classification = "unfilled" if result.entry_capture_count >= MAX_ENTRY_CAPTURES else "observing"
        return result
    if remaining:
        result.classification = "observing"
        return result
    result.classification = "captured" if lifecycle_return(
        entry_price=entry_price or 0.0,
        exits=result.exit_fills,
        quantity=quantity,
        leg_count=legs_count,
        multiplier=multiplier,
    ) is not None else "unmeasurable"
    if result.executable_peak_return is not None:
        realized = lifecycle_return(
            entry_price=entry_price or 0.0,
            exits=result.exit_fills,
            quantity=quantity,
            leg_count=legs_count,
            multiplier=multiplier,
        )
        result.giveback = result.executable_peak_return - (realized or 0.0)
    result.exit_fee = FEE_PER_CONTRACT_LEG * legs_count * quantity
    return result


def _append_exit(
    result: LifecycleResult,
    capture: QuoteCapture,
    quantity: int,
    price: float,
    reason: str,
) -> None:
    if quantity > 0:
        result.exit_fills.append(ExitFill(capture.observed_at, quantity, price, reason, capture.session_number))


def _mark_return(entry: float, mark: float, leg_count: int, multiplier: int) -> float:
    # One-contract round-trip cost is included for MFE/MAE too, preserving a
    # conservative comparable basis even before the staged position is closed.
    entry_cash = entry * multiplier + FEE_PER_CONTRACT_LEG * leg_count
    exit_cash = mark * multiplier - FEE_PER_CONTRACT_LEG * leg_count
    return (exit_cash - entry_cash) / entry_cash if entry_cash > 0 else 0.0
