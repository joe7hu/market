"""Candidate helper functions for QQQ options-history v3 materialization."""

from __future__ import annotations

import hashlib
from datetime import datetime
from math import sqrt
from statistics import mean, pstdev
from typing import Any


def candidate_leg(quote: dict[str, Any], side: str) -> dict[str, Any]:
    bid_size, ask_size = quote.get("bid_size"), quote.get("ask_size")
    size_available = bid_size is not None and bid_size >= 1 and ask_size is not None and ask_size >= 1
    observed_at = quote.get("provider_observed_at")
    available_at = quote.get("available_at")
    return {
        "contract_id": int(quote["contract_id"]), "option_type": quote["option_type"], "side": side,
        "strike": float(quote["strike"]), "bid": quote.get("bid"), "ask": quote.get("ask"),
        "observed_at": observed_at.isoformat() if observed_at is not None else None,
        "available_at": available_at.isoformat() if available_at is not None else None,
        "size_available": size_available,
        "bid_size": bid_size, "ask_size": ask_size, "open_interest": quote.get("open_interest"),
        "volume": quote.get("volume"), "provider_iv": quote.get("provider_iv"),
        "provider_delta": quote.get("provider_delta"),
    }


def spread_short_leg(group: list[dict[str, Any]], long_leg: dict[str, Any]) -> dict[str, Any] | None:
    long_delta = abs(float(long_leg.get("provider_delta") or 0))
    if not 0.35 <= long_delta <= 0.65:
        return None
    candidates = [
        row for row in group
        if row.get("provider_delta") is not None and 0.15 <= abs(float(row["provider_delta"])) <= 0.40
        and ((long_leg["option_type"] == "call" and float(row["strike"]) > float(long_leg["strike"]))
             or (long_leg["option_type"] == "put" and float(row["strike"]) < float(long_leg["strike"])))
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda row: abs(abs(float(row["provider_delta"])) - 0.25))


def non_overlapping_returns(bars: list[dict[str, Any]], dte: int) -> tuple[float, ...]:
    horizon = max(1, min(dte, 120))
    closes = [float(row["close"]) for row in bars if row.get("close") is not None and float(row["close"]) > 0]
    return tuple(closes[index + horizon] / closes[index] - 1.0 for index in range(0, len(closes) - horizon, horizon))


def market_regime(bars: list[dict[str, Any]]) -> str:
    closes = [float(row["close"]) for row in bars if row.get("close") is not None and float(row["close"]) > 0]
    if len(closes) < 200:
        return "unavailable"
    recent_returns = [closes[index] / closes[index - 1] - 1.0 for index in range(len(closes) - 19, len(closes))]
    realized_vol = pstdev(recent_returns) * sqrt(252) if len(recent_returns) > 1 else 0.0
    bucket = "low" if realized_vol < 0.15 else "normal" if realized_vol < 0.30 else "high"
    trend = "above_200d" if closes[-1] >= mean(closes[-200:]) else "below_200d"
    return f"{trend}:{bucket}"


def execution_confidence(legs: list[dict[str, Any]]) -> float:
    scores = []
    for leg in legs:
        bid, ask = leg.get("bid"), leg.get("ask")
        if bid is None or ask is None or float(ask) < float(bid):
            return 0.0
        midpoint = (float(bid) + float(ask)) / 2.0
        if midpoint <= 0 or leg.get("size_available") is not True:
            return 0.0
        scores.append(max(0.0, min(1.0, 1.0 - (float(ask) - float(bid)) / midpoint)))
    return min(scores, default=0.0)


def candidate_seed(generation_id: int, relative_value_id: int, structure: str) -> int:
    digest = hashlib.sha256(f"{generation_id}:{relative_value_id}:{structure}".encode()).hexdigest()
    return int(digest[:16], 16)


def decision_state(value: str) -> str:
    return "READY" if value == "PAPER_READY" else "REJECTED" if value == "REJECT" else "WATCH"
