"""Pure Black-Scholes pricing used by option analysis owners."""

from __future__ import annotations

import math


def black_scholes(
    option_type: str,
    spot: float,
    strike: float,
    years: float,
    risk_free_rate: float,
    iv: float,
) -> float:
    if years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return max(spot - strike, 0.0) if option_type == "call" else max(strike - spot, 0.0)
    sigma_root_t = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * iv * iv) * years) / sigma_root_t
    d2 = d1 - sigma_root_t
    discounted_strike = strike * math.exp(-risk_free_rate * years)
    if option_type == "call":
        return spot * _norm_cdf(d1) - discounted_strike * _norm_cdf(d2)
    return discounted_strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))
