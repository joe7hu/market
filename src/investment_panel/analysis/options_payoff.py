"""Deterministic options payoff scenarios from stored option-chain rows."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from investment_panel.core.db import json_dumps, query_rows


MULTIPLIER = 100


@dataclass(frozen=True)
class OptionLeg:
    option_type: str
    side: str
    strike: float
    premium: float
    quantity: int = 1
    expiry: str | None = None
    dte: int | None = None
    iv: float | None = None

    @property
    def direction(self) -> int:
        return 1 if self.side == "long" else -1

    def as_dict(self) -> dict[str, Any]:
        return {
            "option_type": self.option_type,
            "side": self.side,
            "strike": self.strike,
            "premium": self.premium,
            "quantity": self.quantity,
            "expiry": self.expiry,
            "dte": self.dte,
            "iv": self.iv,
        }


def store_options_payoff_scenarios(con: Any, symbols: list[str], max_scenarios_per_symbol: int = 4) -> int:
    """Store standard deterministic payoff scenarios for symbols with chains.

    These are not recommendations. They are canonical math read models over the
    latest option-chain snapshot: long call, long put, call debit spread, and
    put debit spread when the required strikes exist.
    """

    count = 0
    as_of = datetime.now(UTC).isoformat()
    for symbol in symbols:
        quote = latest_spot(con, symbol)
        if quote is None:
            continue
        rows = latest_chain(con, symbol)
        if not rows:
            continue
        expiry = str(rows[0].get("expiry"))
        calls = sorted([row for row in rows if str(row.get("option_type")).lower() == "call"], key=lambda row: float(row["strike"]))
        puts = sorted([row for row in rows if str(row.get("option_type")).lower() == "put"], key=lambda row: float(row["strike"]))
        if not calls and not puts:
            continue
        scenarios = standard_scenarios(symbol, expiry, quote, calls, puts)[:max_scenarios_per_symbol]
        con.execute("DELETE FROM options_payoff_scenarios WHERE symbol = ? AND source = 'deterministic_chain'", [symbol])
        for scenario in scenarios:
            con.execute(
                """
                INSERT OR REPLACE INTO options_payoff_scenarios
                (id, symbol, as_of, expiry, strategy_type, spot, dte, iv, net_premium,
                 max_profit, max_loss, breakevens, legs, curve, diagnostics, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    stable_id(f"{symbol}:{as_of}:{scenario['strategy_type']}:{expiry}"),
                    symbol,
                    as_of,
                    expiry,
                    scenario["strategy_type"],
                    scenario["spot"],
                    scenario["dte"],
                    scenario["iv"],
                    scenario["net_premium"],
                    scenario["max_profit"],
                    scenario["max_loss"],
                    json_dumps(scenario["breakevens"]),
                    json_dumps(scenario["legs"]),
                    json_dumps(scenario["curve"]),
                    json_dumps(scenario["diagnostics"]),
                    "deterministic_chain",
                ],
            )
            count += 1
    return count


def standard_scenarios(
    symbol: str,
    expiry: str,
    spot: float,
    calls: list[dict[str, Any]],
    puts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    atm_call_index = nearest_index(calls, spot)
    atm_put_index = nearest_index(puts, spot)
    if atm_call_index is not None:
        atm_call = calls[atm_call_index]
        scenarios.append(evaluate_strategy(symbol, "long_call", spot, [leg_from_row(atm_call, "long")]))
        if atm_call_index + 1 < len(calls):
            scenarios.append(
                evaluate_strategy(
                    symbol,
                    "call_debit_spread",
                    spot,
                    [leg_from_row(atm_call, "long"), leg_from_row(calls[atm_call_index + 1], "short")],
                )
            )
    if atm_put_index is not None:
        atm_put = puts[atm_put_index]
        scenarios.append(evaluate_strategy(symbol, "long_put", spot, [leg_from_row(atm_put, "long")]))
        if atm_put_index > 0:
            scenarios.append(
                evaluate_strategy(
                    symbol,
                    "put_debit_spread",
                    spot,
                    [leg_from_row(atm_put, "long"), leg_from_row(puts[atm_put_index - 1], "short")],
                )
            )
    return scenarios


def evaluate_strategy(
    symbol: str,
    strategy_type: str,
    spot: float,
    legs: list[OptionLeg],
    risk_free_rate: float = 0.043,
    points: int = 41,
) -> dict[str, Any]:
    strikes = [leg.strike for leg in legs]
    min_strike = min(strikes)
    max_strike = max(strikes)
    lower = max(0.01, min(min_strike, spot) * 0.75)
    upper = max(max_strike, spot) * 1.25
    step = (upper - lower) / max(points - 1, 1)
    dte = max(as_int(first_non_null([leg.dte for leg in legs])) or inferred_dte(legs), 0)
    iv = average_iv(legs)
    curve = []
    expiry_values = []
    theoretical_values = []
    for index in range(points):
        underlying = lower + step * index
        expiry_pnl = strategy_expiry_pnl(legs, underlying)
        theoretical_pnl = strategy_theoretical_pnl(legs, underlying, max(dte, 1) / 365, risk_free_rate, iv)
        curve.append(
            {
                "underlying": round(underlying, 4),
                "expiry_pnl": round(expiry_pnl, 4),
                "theoretical_pnl": round(theoretical_pnl, 4),
            }
        )
        expiry_values.append(expiry_pnl)
        theoretical_values.append(theoretical_pnl)
    net_premium = sum(leg.direction * leg.premium * leg.quantity * MULTIPLIER for leg in legs)
    max_profit, max_loss = payoff_extremes(strategy_type, legs, expiry_values)
    return {
        "symbol": symbol,
        "strategy_type": strategy_type,
        "spot": spot,
        "dte": dte,
        "iv": iv,
        "net_premium": round(net_premium, 4),
        "max_profit": max_profit,
        "max_loss": max_loss,
        "breakevens": breakevens(curve),
        "legs": [leg.as_dict() for leg in legs],
        "curve": curve,
        "diagnostics": {
            "pricing_model": "black_scholes",
            "risk_free_rate": risk_free_rate,
            "point_count": len(curve),
            "spot_in_range": lower <= spot <= upper,
            "note": "Deterministic payoff math from stored chain rows; no trade recommendation.",
        },
    }


def strategy_expiry_pnl(legs: list[OptionLeg], underlying: float) -> float:
    total = 0.0
    for leg in legs:
        intrinsic = max(underlying - leg.strike, 0.0) if leg.option_type == "call" else max(leg.strike - underlying, 0.0)
        total += leg.direction * (intrinsic - leg.premium) * leg.quantity * MULTIPLIER
    return total


def strategy_theoretical_pnl(legs: list[OptionLeg], underlying: float, years: float, risk_free_rate: float, fallback_iv: float) -> float:
    total = 0.0
    for leg in legs:
        price = black_scholes(leg.option_type, underlying, leg.strike, years, risk_free_rate, fallback_iv)
        total += leg.direction * (price - leg.premium) * leg.quantity * MULTIPLIER
    return total


def payoff_extremes(strategy_type: str, legs: list[OptionLeg], sampled_values: list[float]) -> tuple[float | None, float | None]:
    if strategy_type == "long_call":
        premium = sum(leg.premium * leg.quantity * MULTIPLIER for leg in legs)
        return None, round(-premium, 4)
    if strategy_type == "long_put":
        leg = legs[0]
        return round((leg.strike - leg.premium) * leg.quantity * MULTIPLIER, 4), round(-leg.premium * leg.quantity * MULTIPLIER, 4)
    return bounded_extreme(max(sampled_values)), bounded_extreme(min(sampled_values))


def black_scholes(option_type: str, spot: float, strike: float, years: float, risk_free_rate: float, iv: float) -> float:
    if years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return max(spot - strike, 0.0) if option_type == "call" else max(strike - spot, 0.0)
    sigma_root_t = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * iv * iv) * years) / sigma_root_t
    d2 = d1 - sigma_root_t
    discounted_strike = strike * math.exp(-risk_free_rate * years)
    if option_type == "call":
        return spot * norm_cdf(d1) - discounted_strike * norm_cdf(d2)
    return discounted_strike * norm_cdf(-d2) - spot * norm_cdf(-d1)


def norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def breakevens(curve: list[dict[str, float]]) -> list[float]:
    breaks: list[float] = []
    for left, right in zip(curve, curve[1:], strict=False):
        left_pnl = left["expiry_pnl"]
        right_pnl = right["expiry_pnl"]
        if left_pnl == 0:
            breaks.append(left["underlying"])
        if left_pnl * right_pnl < 0:
            distance = abs(left_pnl) / (abs(left_pnl) + abs(right_pnl))
            breaks.append(left["underlying"] + (right["underlying"] - left["underlying"]) * distance)
    return [round(value, 4) for value in breaks]


def latest_spot(con: Any, symbol: str) -> float | None:
    rows = query_rows(con, "SELECT price FROM quotes_intraday WHERE symbol = ? ORDER BY observed_at DESC LIMIT 1", [symbol])
    if not rows:
        return None
    return as_float(rows[0].get("price"))


def latest_chain(con: Any, symbol: str) -> list[dict[str, Any]]:
    return query_rows(
        con,
        """
        SELECT symbol, expiry, strike, option_type, bid, ask, mid, iv, delta, dte
        FROM (
            SELECT c.*, e.dte
            FROM options_chain c
            LEFT JOIN options_expiries e
              ON e.symbol = c.symbol
             AND e.expiry = c.expiry
             AND e.source = c.source
            WHERE c.symbol = ?
            QUALIFY dense_rank() OVER (PARTITION BY c.symbol ORDER BY c.observed_at DESC, c.expiry ASC) = 1
        )
        ORDER BY expiry, strike, option_type
        """,
        [symbol],
    )


def leg_from_row(row: dict[str, Any], side: str) -> OptionLeg:
    return OptionLeg(
        option_type=str(row.get("option_type") or "").lower(),
        side=side,
        strike=float(row["strike"]),
        premium=option_premium(row, side),
        quantity=1,
        expiry=str(row.get("expiry")) if row.get("expiry") is not None else None,
        dte=as_int(row.get("dte")),
        iv=as_float(row.get("iv")),
    )


def option_premium(row: dict[str, Any], side: str) -> float:
    if side == "long":
        return as_float(row.get("ask")) or as_float(row.get("mid")) or as_float(row.get("bid")) or 0.0
    return as_float(row.get("bid")) or as_float(row.get("mid")) or as_float(row.get("ask")) or 0.0


def nearest_index(rows: list[dict[str, Any]], spot: float) -> int | None:
    if not rows:
        return None
    return min(range(len(rows)), key=lambda index: abs(float(rows[index]["strike"]) - spot))


def average_iv(legs: list[OptionLeg]) -> float:
    values = [leg.iv for leg in legs if leg.iv is not None and leg.iv > 0]
    if not values:
        return 0.3
    return sum(values) / len(values)


def inferred_dte(legs: list[OptionLeg]) -> int:
    expiry = next((leg.expiry for leg in legs if leg.expiry), None)
    if not expiry:
        return 30
    try:
        expiry_date = datetime.fromisoformat(expiry[:10]).date()
    except ValueError:
        return 30
    return max((expiry_date - datetime.now(UTC).date()).days, 0)


def bounded_extreme(value: float) -> float | None:
    if not math.isfinite(value):
        return None
    return round(value, 4)


def first_non_null(values: list[Any]) -> Any:
    return next((value for value in values if value is not None), None)


def as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
