"""Option greeks resolution and a scipy-free Black-Scholes model.

When a data provider ships incomplete greeks the radar falls back to a
TradingView cross-source match and finally to this analytic model, tagging the
result with its provenance so downstream quality scoring can flag modeled greeks.
"""

from __future__ import annotations

import math
from typing import Any

from investment_panel.core.options_radar.coerce import _number
from investment_panel.core.options_radar.constants import (
    DEFAULT_OPTION_RISK_FREE_RATE,
    MIN_OPTION_MODEL_DTE_DAYS,
    MIN_OPTION_MODEL_IV,
)


def _resolve_option_greeks(
    row: dict[str, Any],
    *,
    option_type: str,
    underlying_price: float | None,
    strike: float | None,
    dte: int | None,
    iv: float | None,
) -> dict[str, Any]:
    provider_values = {name: _number(row.get(name)) for name in ("delta", "gamma", "theta", "vega")}
    matched_values = {name: _number(row.get(f"tradingview_{name}")) for name in ("delta", "gamma", "theta", "vega")}
    if all(value is not None for value in provider_values.values()):
        return {**provider_values, "source": "provider"}

    resolved: dict[str, float | None] = {}
    used_match = False
    for name, value in provider_values.items():
        if value is not None:
            resolved[name] = value
            continue
        matched = matched_values[name]
        if matched is not None:
            resolved[name] = matched
            used_match = True
        else:
            resolved[name] = None

    used_model = False
    if any(value is None for value in resolved.values()):
        modeled_values = _black_scholes_greeks(option_type, underlying_price, strike, dte, iv)
        for name, value in resolved.items():
            if value is None and modeled_values.get(name) is not None:
                resolved[name] = modeled_values[name]
                used_model = True

    if used_match and used_model:
        greek_source = "mixed_fallback"
    elif used_model:
        greek_source = "black_scholes_model"
    elif used_match:
        greek_source = "tradingview_match"
    else:
        greek_source = "provider"
    return {**resolved, "source": greek_source}


def _black_scholes_greeks(
    option_type: str,
    spot: float | None,
    strike: float | None,
    dte: int | None,
    iv: float | None,
    *,
    risk_free_rate: float = DEFAULT_OPTION_RISK_FREE_RATE,
) -> dict[str, float]:
    if not option_type or spot is None or strike is None or dte is None or iv is None:
        return {}
    if option_type not in {"call", "put"} or spot <= 0 or strike <= 0 or dte < 0 or iv < 0:
        return {}
    if not all(math.isfinite(value) for value in (spot, strike, float(dte), iv, risk_free_rate)):
        return {}
    model_dte = _option_model_dte(dte)
    model_iv = _option_model_iv(iv)
    if model_dte is None or model_iv is None:
        return {}
    years = model_dte / 365.0
    sqrt_years = math.sqrt(years)
    sigma_sqrt_t = model_iv * sqrt_years
    if sigma_sqrt_t <= 0:
        return {}

    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * model_iv * model_iv) * years) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    pdf = _norm_pdf(d1)
    discount = math.exp(-risk_free_rate * years)
    if option_type == "call":
        delta = _norm_cdf(d1)
        theta_annual = -((spot * pdf * model_iv) / (2 * sqrt_years)) - (risk_free_rate * strike * discount * _norm_cdf(d2))
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_annual = -((spot * pdf * model_iv) / (2 * sqrt_years)) + (risk_free_rate * strike * discount * _norm_cdf(-d2))
    gamma = pdf / (spot * sigma_sqrt_t)
    theta = theta_annual / 365.0
    vega = spot * pdf * sqrt_years
    return {
        "delta": round(delta, 6),
        "gamma": round(gamma, 6),
        "theta": round(theta, 6),
        "vega": round(vega, 6),
    }


def _option_model_dte(dte: int | None) -> int | None:
    if dte is None or dte < 0:
        return None
    return max(dte, MIN_OPTION_MODEL_DTE_DAYS)


def _option_model_iv(iv: float | None) -> float | None:
    if iv is None or iv < 0:
        return None
    return max(iv, MIN_OPTION_MODEL_IV)


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _norm_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)
