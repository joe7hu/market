"""Pure cash-secured-put payoff, collateral, and tail-risk calculations."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from statistics import NormalDist


@dataclass(frozen=True)
class CashSecuredPutInputs:
    spot: float
    strike: float
    dte: int
    bid: float
    ask: float
    delta: float
    multiplier: int = 100
    fee_per_contract: float = 0.65
    annualized_volatility: float | None = None


@dataclass(frozen=True)
class CashSecuredPutResult:
    entry_credit: float
    secured_cash: float
    max_profit: float
    max_loss: float
    break_even: float
    effective_assignment_price: float
    return_on_collateral: float
    annualized_return_on_collateral: float
    probability_profit: float
    probability_assignment: float
    probability_touch: float
    tail_cvar: float
    stress_losses: dict[str, float]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def evaluate_cash_secured_put(inputs: CashSecuredPutInputs) -> CashSecuredPutResult | None:
    """Evaluate one fully cash-secured short put at a conservative bid fill.

    Probabilities are explicitly provisional: provider delta supplies the
    risk-neutral assignment proxy, while the available volatility supplies a
    lognormal probability-of-profit estimate. Calibration replaces these
    proxies once forward outcomes mature.
    """

    numbers = (inputs.spot, inputs.strike, inputs.bid, inputs.ask, inputs.delta)
    if (
        any(not math.isfinite(value) for value in numbers)
        or inputs.spot <= 0
        or inputs.strike <= 0
        or inputs.dte <= 0
        or inputs.bid <= 0
        or inputs.ask < inputs.bid
        or inputs.multiplier <= 0
        or inputs.fee_per_contract < 0
        or not -1 <= inputs.delta <= 0
    ):
        return None

    gross_credit = inputs.bid * inputs.multiplier
    entry_credit = max(0.0, gross_credit - inputs.fee_per_contract)
    if entry_credit <= 0:
        return None
    secured_cash = inputs.strike * inputs.multiplier - entry_credit
    if secured_cash <= 0:
        return None
    basis = secured_cash / inputs.multiplier
    return_on_collateral = entry_credit / secured_cash
    annualized = (1.0 + return_on_collateral) ** (365.0 / inputs.dte) - 1.0
    probability_assignment = min(1.0, max(0.0, abs(inputs.delta)))
    probability_touch = min(1.0, 2.0 * probability_assignment)
    probability_profit = _probability_above(
        inputs.spot,
        basis,
        inputs.dte,
        inputs.annualized_volatility,
        fallback=1.0 - probability_assignment,
    )

    stress_prices = {
        "down_10": inputs.spot * 0.90,
        "down_20": inputs.spot * 0.80,
        "down_30": inputs.spot * 0.70,
        "zero": 0.0,
    }
    stress_losses = {
        key: round(min(secured_cash, max(0.0, (inputs.strike - price) * inputs.multiplier - entry_credit)), 2)
        for key, price in stress_prices.items()
    }
    tail_price = _tail_price(inputs.spot, inputs.dte, inputs.annualized_volatility)
    tail_cvar = min(secured_cash, max(0.0, (inputs.strike - tail_price) * inputs.multiplier - entry_credit))

    return CashSecuredPutResult(
        entry_credit=round(entry_credit, 2),
        secured_cash=round(secured_cash, 2),
        max_profit=round(entry_credit, 2),
        max_loss=round(secured_cash, 2),
        break_even=round(basis, 4),
        effective_assignment_price=round(basis, 4),
        return_on_collateral=round(return_on_collateral, 6),
        annualized_return_on_collateral=round(annualized, 6),
        probability_profit=round(probability_profit, 6),
        probability_assignment=round(probability_assignment, 6),
        probability_touch=round(probability_touch, 6),
        tail_cvar=round(tail_cvar, 2),
        stress_losses=stress_losses,
    )


def _probability_above(
    spot: float,
    threshold: float,
    dte: int,
    annualized_volatility: float | None,
    *,
    fallback: float,
) -> float:
    if annualized_volatility is None or not math.isfinite(annualized_volatility) or annualized_volatility <= 0:
        return min(1.0, max(0.0, fallback))
    sigma = annualized_volatility * math.sqrt(dte / 365.0)
    if sigma <= 0:
        return float(spot > threshold)
    z = (math.log(threshold / spot) + 0.5 * sigma * sigma) / sigma
    return min(1.0, max(0.0, 1.0 - NormalDist().cdf(z)))


def _tail_price(spot: float, dte: int, annualized_volatility: float | None) -> float:
    volatility = annualized_volatility if annualized_volatility and annualized_volatility > 0 else 0.50
    sigma = volatility * math.sqrt(dte / 365.0)
    # Conservative fifth-percentile terminal price. This is a stress proxy,
    # not a calibrated expected-shortfall claim.
    return spot * math.exp(-0.5 * sigma * sigma + NormalDist().inv_cdf(0.05) * sigma)
