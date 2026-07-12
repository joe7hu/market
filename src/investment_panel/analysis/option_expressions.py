"""Pure empirical expectancy for supported long-option trade expressions."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class LongOptionInputs:
    option_type: str
    spot: float
    strike: float
    ask: float
    bid: float
    historical_horizon_returns: tuple[float, ...]
    multiplier: int = 100


@dataclass(frozen=True)
class DebitSpreadInputs:
    spot: float
    long_strike: float
    short_strike: float
    long_ask: float
    short_bid: float
    historical_horizon_returns: tuple[float, ...]
    multiplier: int = 100


@dataclass(frozen=True)
class ExpressionResult:
    entry_cost: float
    max_loss: float
    max_profit: float | None
    break_even: float
    expected_value: float
    expected_loss: float
    risk_adjusted_expectancy: float
    probability_profit: float
    scenario_count: int
    required_2x_price: float | None = None
    required_5x_price: float | None = None
    required_10x_price: float | None = None
    target_reasons: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def evaluate_long_option(inputs: LongOptionInputs) -> ExpressionResult | None:
    values = (inputs.spot, inputs.strike, inputs.ask, inputs.bid)
    if (
        inputs.option_type not in {"call", "put"}
        or any(not math.isfinite(value) for value in values)
        or inputs.spot <= 0
        or inputs.strike <= 0
        or inputs.ask <= 0
        or inputs.bid < 0
        or inputs.bid > inputs.ask
        or inputs.multiplier <= 0
        or not inputs.historical_horizon_returns
    ):
        return None
    entry = inputs.ask * inputs.multiplier
    spread_cost = (inputs.ask - inputs.bid) * inputs.multiplier
    scenario_pnls = []
    for horizon_return in inputs.historical_horizon_returns:
        terminal = inputs.spot * (1 + horizon_return)
        intrinsic = max(0.0, terminal - inputs.strike) if inputs.option_type == "call" else max(0.0, inputs.strike - terminal)
        scenario_pnls.append(intrinsic * inputs.multiplier - entry - spread_cost)
    expected_value, expected_loss, probability_profit = _moments(scenario_pnls)
    targets, reasons = _long_targets(inputs)
    return ExpressionResult(
        entry_cost=round(entry, 2),
        max_loss=round(entry, 2),
        max_profit=None if inputs.option_type == "call" else round(max(0.0, inputs.strike * inputs.multiplier - entry), 2),
        break_even=round(inputs.strike + inputs.ask if inputs.option_type == "call" else inputs.strike - inputs.ask, 4),
        expected_value=round(expected_value, 2),
        expected_loss=round(expected_loss, 2),
        risk_adjusted_expectancy=expected_value / entry,
        probability_profit=probability_profit,
        scenario_count=len(scenario_pnls),
        required_2x_price=targets[2],
        required_5x_price=targets[5],
        required_10x_price=targets[10],
        target_reasons=reasons,
    )


def evaluate_call_debit_spread(inputs: DebitSpreadInputs) -> ExpressionResult | None:
    values = (inputs.spot, inputs.long_strike, inputs.short_strike, inputs.long_ask, inputs.short_bid)
    debit = inputs.long_ask - inputs.short_bid
    width = inputs.short_strike - inputs.long_strike
    if (
        any(not math.isfinite(value) for value in values)
        or inputs.spot <= 0
        or inputs.long_strike <= 0
        or width <= 0
        or debit <= 0
        or debit >= width
        or inputs.multiplier <= 0
        or not inputs.historical_horizon_returns
    ):
        return None
    entry = debit * inputs.multiplier
    max_profit = (width - debit) * inputs.multiplier
    scenario_pnls = []
    for horizon_return in inputs.historical_horizon_returns:
        terminal = inputs.spot * (1 + horizon_return)
        payoff = min(width, max(0.0, terminal - inputs.long_strike))
        scenario_pnls.append(payoff * inputs.multiplier - entry)
    expected_value, expected_loss, probability_profit = _moments(scenario_pnls)
    targets: dict[int, float | None] = {}
    reasons: dict[str, str] = {}
    for multiple in (2, 5, 10):
        target = inputs.long_strike + debit * multiple
        if debit * multiple <= width:
            targets[multiple] = round(target, 4)
        else:
            targets[multiple] = None
            reasons[f"{multiple}x"] = "target_not_attainable"
    return ExpressionResult(
        entry_cost=round(entry, 2),
        max_loss=round(entry, 2),
        max_profit=round(max_profit, 2),
        break_even=round(inputs.long_strike + debit, 4),
        expected_value=round(expected_value, 2),
        expected_loss=round(expected_loss, 2),
        risk_adjusted_expectancy=expected_value / entry,
        probability_profit=probability_profit,
        scenario_count=len(scenario_pnls),
        required_2x_price=targets[2],
        required_5x_price=targets[5],
        required_10x_price=targets[10],
        target_reasons=reasons,
    )


def _moments(pnls: list[float]) -> tuple[float, float, float]:
    expected = sum(pnls) / len(pnls)
    losses = [-pnl for pnl in pnls if pnl < 0]
    expected_loss = sum(losses) / len(pnls)
    probability_profit = sum(pnl > 0 for pnl in pnls) / len(pnls)
    return expected, expected_loss, probability_profit


def _long_targets(inputs: LongOptionInputs) -> tuple[dict[int, float | None], dict[str, str]]:
    targets: dict[int, float | None] = {}
    reasons: dict[str, str] = {}
    for multiple in (2, 5, 10):
        if inputs.option_type == "call":
            targets[multiple] = round(inputs.strike + inputs.ask * multiple, 4)
        else:
            target = inputs.strike - inputs.ask * multiple
            targets[multiple] = round(target, 4) if target >= 0 else None
            if target < 0:
                reasons[f"{multiple}x"] = "target_not_attainable"
    return targets, reasons
