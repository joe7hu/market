"""Pure option-chain signal builders.

Persistence belongs to :mod:`investment_panel.database.options_analysis` and
:mod:`investment_panel.database.panel_watchlist`. This module only transforms
already loaded PostgreSQL rows into option summary values.
"""

from __future__ import annotations

import math
from datetime import datetime
from statistics import mean
from typing import Any


TRADINGVIEW_UNAVAILABLE_SIGNALS = [
    {"signal": "open_interest", "reason": "TradingView OpenCLI options-chain output does not include open interest."},
    {"signal": "volume", "reason": "TradingView OpenCLI options-chain output does not include contract volume."},
    {"signal": "gex_regime", "reason": "Gamma exposure requires open interest or position size by strike."},
    {"signal": "call_wall", "reason": "Call walls require call open interest or gamma exposure by strike."},
    {"signal": "put_wall", "reason": "Put walls require put open interest or gamma exposure by strike."},
    {"signal": "gamma_flip", "reason": "Gamma flip requires cumulative gamma exposure by strike."},
    {"signal": "max_pain", "reason": "Max pain requires open interest by strike."},
    {"signal": "unusual_volume", "reason": "Unusual volume requires live or historical contract volume."},
]
_UNCOMPUTED_POSITIONING_SIGNALS = [
    signal for signal in TRADINGVIEW_UNAVAILABLE_SIGNALS
    if signal["signal"] not in {"open_interest", "volume"}
]

__all__ = [
    "TRADINGVIEW_UNAVAILABLE_SIGNALS",
    "build_expiry_signal",
    "build_ticker_signal",
    "unavailable_signals_for_source",
]


def unavailable_signals_for_source(source: str | None) -> list[dict[str, Any]]:
    """Return signals unavailable for the selected provider."""

    if str(source or "").lower() == "tradingview":
        return TRADINGVIEW_UNAVAILABLE_SIGNALS
    return _UNCOMPUTED_POSITIONING_SIGNALS


def _provider_limitation_note(source: str | None) -> str:
    if str(source or "").lower() == "tradingview":
        return "TradingView V1 lacks OI/volume, so positioning walls are not calculated."
    return "Positioning walls (GEX/max-pain) are not calculated from this snapshot yet."


def build_expiry_signal(
    symbol: str,
    expiry: str,
    source: str,
    chain_rows: list[dict[str, Any]],
    expiry_meta: dict[str, Any],
    quote: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    rows = [row for row in chain_rows if _number(row.get("strike")) is not None]
    if not rows:
        return None
    spot = _number((quote or {}).get("price")) or _infer_spot(rows)
    atm_strike = _atm_strike(rows, spot)
    if atm_strike is None:
        return None
    atm_rows = [row for row in rows if _number(row.get("strike")) == atm_strike]
    calls = [row for row in rows if str(row.get("option_type") or "").lower() == "call"]
    puts = [row for row in rows if str(row.get("option_type") or "").lower() == "put"]
    atm_iv = _average([_number(row.get("iv")) for row in atm_rows])
    atm_call = _best_at_strike(calls, atm_strike)
    atm_put = _best_at_strike(puts, atm_strike)
    dte = _integer(expiry_meta.get("dte") if expiry_meta else rows[0].get("dte"))
    if dte is None:
        dte = _expiry_dte(expiry, max((str(row.get("observed_at") or "") for row in rows), default=None))
    expected_move = _sum_present(_number((atm_call or {}).get("mid")), _number((atm_put or {}).get("mid")))
    if expected_move is None:
        expected_move = _iv_implied_move(spot, atm_iv, dte)
    expected_move_pct = expected_move / spot if expected_move is not None and spot else None
    call_25 = _closest_by_delta(calls, 0.25)
    put_25 = _closest_by_delta(puts, -0.25)
    put_call_iv_skew = _diff(_number((put_25 or {}).get("iv")), _number((call_25 or {}).get("iv")))
    call_spread_pct = _average_spread_pct(calls)
    put_spread_pct = _average_spread_pct(puts)
    spread_pct = _average([call_spread_pct, put_spread_pct])
    hedge_put = _closest_by_delta(
        [row for row in puts if spot is None or (_number(row.get("strike")) or 0) <= spot], -0.25,
    ) or put_25
    covered_call = _closest_by_delta(
        [row for row in calls if spot is None or (_number(row.get("strike")) or 0) >= spot], 0.30,
    ) or call_25
    as_of = max(str(row.get("observed_at") or "") for row in rows if row.get("observed_at")) or datetime.utcnow().isoformat()
    availability_values = [row.get("available_at") for row in rows if row.get("available_at") is not None]
    return {
        "symbol": symbol,
        "expiry": expiry,
        "as_of": as_of,
        "available_at": max(availability_values) if availability_values else None,
        "source": source,
        "dte": dte,
        "spot": spot,
        "contract_count": _integer(expiry_meta.get("contracts_count")) if expiry_meta else None,
        "chain_rows": len(rows),
        "atm_strike": atm_strike,
        "atm_iv": atm_iv,
        "expected_move": expected_move,
        "expected_move_pct": expected_move_pct,
        "put_call_iv_skew": put_call_iv_skew,
        "call_spread_pct": call_spread_pct,
        "put_spread_pct": put_spread_pct,
        "spread_quality": _spread_quality(spread_pct),
        "liquidity_score": _liquidity_score(spread_pct, len(rows)),
        "hedge_put_strike": _number((hedge_put or {}).get("strike")),
        "hedge_put_mid": _number((hedge_put or {}).get("mid")),
        "covered_call_strike": _number((covered_call or {}).get("strike")),
        "covered_call_mid": _number((covered_call or {}).get("mid")),
        "unavailable_signals": unavailable_signals_for_source(source),
        "raw": {
            "atm_contracts": [_contract_label(row) for row in atm_rows],
            "capability_note": _provider_limitation_note(source),
        },
    }


def build_ticker_signal(symbol: str, source: str, expiry_signals: list[dict[str, Any]]) -> dict[str, Any]:
    usable = sorted(
        [row for row in expiry_signals if row.get("expected_move_pct") is not None or row.get("atm_iv") is not None],
        key=lambda row: (_integer(row.get("dte")) if _integer(row.get("dte")) is not None else 99999, str(row.get("expiry") or "")),
    )
    selected = next(
        (row for row in usable if (_integer(row.get("dte")) or 0) >= 1),
        usable[0] if usable else expiry_signals[0],
    )
    skew = _number(selected.get("put_call_iv_skew"))
    hedge_mid = _number(selected.get("hedge_put_mid"))
    hedge_strike = _number(selected.get("hedge_put_strike"))
    call_mid = _number(selected.get("covered_call_mid"))
    call_strike = _number(selected.get("covered_call_strike"))
    expiry = selected.get("expiry")
    return {
        "symbol": symbol,
        "as_of": selected.get("as_of") or datetime.utcnow().isoformat(),
        "available_at": selected.get("available_at"),
        "source": source,
        "status": "loaded" if usable else "limited",
        "nearest_expiry": expiry,
        "nearest_dte": _integer(selected.get("dte")),
        "atm_iv": _number(selected.get("atm_iv")),
        "iv_regime": _iv_regime(_number(selected.get("atm_iv"))),
        "expected_move": _number(selected.get("expected_move")),
        "expected_move_pct": _number(selected.get("expected_move_pct")),
        "skew_signal": _skew_signal(skew),
        "put_call_iv_skew": skew,
        "spread_quality": selected.get("spread_quality"),
        "liquidity_score": _number(selected.get("liquidity_score")),
        "hedge_summary": _contract_summary("Put", hedge_strike, hedge_mid, expiry),
        "income_summary": _contract_summary("Call", call_strike, call_mid, expiry),
        "unavailable_signals": unavailable_signals_for_source(source),
        "raw": {
            "expiry_count": len(expiry_signals),
            "selected_expiry": expiry,
            "provider_limitation": _provider_limitation_note(source),
        },
    }


def _expiry_dte(expiry: Any, reference: Any = None) -> int | None:
    try:
        exp = datetime.fromisoformat(str(expiry)[:10]).date()
    except (TypeError, ValueError):
        return None
    try:
        ref = datetime.fromisoformat(str(reference)[:10]).date() if reference else datetime.utcnow().date()
    except (TypeError, ValueError):
        ref = datetime.utcnow().date()
    return (exp - ref).days


def _iv_implied_move(spot: float | None, atm_iv: float | None, dte: int | None) -> float | None:
    if not spot or atm_iv is None or not dte or dte <= 0:
        return None
    return spot * atm_iv * math.sqrt(dte / 365.0)


def _atm_strike(rows: list[dict[str, Any]], spot: float | None) -> float | None:
    strikes = sorted({_number(row.get("strike")) for row in rows if _number(row.get("strike")) is not None})
    if not strikes:
        return None
    if spot:
        return min(strikes, key=lambda strike: abs(strike - spot))
    delta_rows = [row for row in rows if _number(row.get("delta")) is not None]
    if delta_rows:
        return _number(min(delta_rows, key=lambda row: abs(abs(_number(row.get("delta")) or 0) - 0.5)).get("strike"))
    return strikes[len(strikes) // 2]


def _infer_spot(rows: list[dict[str, Any]]) -> float | None:
    atm = _atm_strike(rows, None)
    if atm is None:
        return None
    call = _best_at_strike([row for row in rows if str(row.get("option_type") or "").lower() == "call"], atm)
    put = _best_at_strike([row for row in rows if str(row.get("option_type") or "").lower() == "put"], atm)
    call_mid = _number((call or {}).get("mid"))
    put_mid = _number((put or {}).get("mid"))
    if call_mid is not None and put_mid is not None:
        return atm + call_mid - put_mid
    return atm


def _best_at_strike(rows: list[dict[str, Any]], strike: float) -> dict[str, Any] | None:
    matches = [row for row in rows if _number(row.get("strike")) == strike]
    return max(matches, key=lambda row: _number(row.get("mid")) or 0) if matches else None


def _closest_by_delta(rows: list[dict[str, Any]], target: float) -> dict[str, Any] | None:
    candidates = [row for row in rows if _number(row.get("delta")) is not None]
    return min(candidates, key=lambda row: abs((_number(row.get("delta")) or 0) - target)) if candidates else None


def _average_spread_pct(rows: list[dict[str, Any]]) -> float | None:
    spreads = []
    for row in rows:
        bid = _number(row.get("bid"))
        ask = _number(row.get("ask"))
        mid = _number(row.get("mid"))
        if bid is None or ask is None or mid is None or mid <= 0:
            continue
        spreads.append(max(0, ask - bid) / mid)
    return _average(spreads)


def _spread_quality(spread_pct: float | None) -> str:
    if spread_pct is None:
        return "unknown"
    if spread_pct <= 0.08:
        return "tight"
    if spread_pct <= 0.18:
        return "usable"
    return "wide"


def _liquidity_score(spread_pct: float | None, chain_rows: int) -> float | None:
    if spread_pct is None:
        return None
    spread_component = max(0.0, 100.0 - spread_pct * 350.0)
    coverage_component = min(100.0, max(0.0, chain_rows / 60.0 * 100.0))
    return round((spread_component * 0.75) + (coverage_component * 0.25), 2)


def _iv_regime(iv: float | None) -> str:
    if iv is None:
        return "unknown"
    if iv < 0.25:
        return "low"
    if iv > 0.60:
        return "elevated"
    return "normal"


def _skew_signal(skew: float | None) -> str:
    if skew is None:
        return "unknown"
    if skew >= 0.05:
        return "put premium"
    if skew <= -0.03:
        return "call premium"
    return "neutral"


def _contract_summary(label: str, strike: float | None, mid: float | None, expiry: Any) -> str:
    if strike is None or mid is None:
        return "No usable contract."
    return f"{label} {strike:g} exp {expiry}: mid ${mid:.2f}"


def _contract_label(row: dict[str, Any]) -> str:
    return str(row.get("contract_symbol") or row.get("raw_symbol") or row.get("symbol") or "")


def _sum_present(*values: float | None) -> float | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _diff(left: float | None, right: float | None) -> float | None:
    return left - right if left is not None and right is not None else None


def _average(values: list[float | None]) -> float | None:
    finite = [value for value in values if value is not None]
    return mean(finite) if finite else None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
