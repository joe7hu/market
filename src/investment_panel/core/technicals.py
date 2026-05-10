"""Technical feature calculation."""

from __future__ import annotations

from typing import Any

import pandas as pd

from investment_panel.core.db import json_dumps


def calculate_features(prices: pd.DataFrame) -> dict[str, Any]:
    ordered = prices.sort_values("date").copy()
    close = ordered["close"].astype(float)
    volume = ordered["volume"].astype(float)
    last = ordered.iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma50 = close.rolling(50).mean().iloc[-1]
    ma200 = close.rolling(min(200, len(close))).mean().iloc[-1]
    high_252 = close.rolling(min(252, len(close))).max().iloc[-1]
    low_252 = close.rolling(min(252, len(close))).min().iloc[-1]
    ret_20 = close.iloc[-1] / close.iloc[-min(21, len(close))] - 1 if len(close) > 1 else 0
    ret_60 = close.iloc[-1] / close.iloc[-min(61, len(close))] - 1 if len(close) > 1 else 0
    vol_ratio = volume.iloc[-20:].mean() / max(volume.iloc[-60:].mean(), 1)
    drawdown = close.iloc[-1] / max(high_252, 1) - 1
    recovery = (close.iloc[-1] - low_252) / max(high_252 - low_252, 1e-9)
    trend_score = score_technical(close.iloc[-1], ma20, ma50, ma200, ret_20, ret_60, vol_ratio, recovery)
    return {
        "date": str(last["date"]),
        "close": float(close.iloc[-1]),
        "ma20": float(ma20) if pd.notna(ma20) else None,
        "ma50": float(ma50) if pd.notna(ma50) else None,
        "ma200": float(ma200) if pd.notna(ma200) else None,
        "return_20d": float(ret_20),
        "return_60d": float(ret_60),
        "volume_ratio_20_60": float(vol_ratio),
        "drawdown_from_high": float(drawdown),
        "range_recovery": float(recovery),
        "technical_score": trend_score,
        "source": str(last.get("source", "")),
        "price_source": str(last.get("source", "")),
    }


def score_technical(close: float, ma20: float, ma50: float, ma200: float, ret20: float, ret60: float, vol_ratio: float, recovery: float) -> float:
    score = 0.0
    score += 18 if close > ma20 else 4
    score += 18 if close > ma50 else 4
    score += 14 if close > ma200 else 3
    score += min(max(ret20 * 120, -10), 16)
    score += min(max(ret60 * 80, -10), 16)
    score += min(max((vol_ratio - 0.8) * 18, 0), 10)
    score += min(max(recovery * 12, 0), 12)
    return round(max(0.0, min(100.0, score)), 2)


def compute_and_store(con: Any, symbol: str) -> dict[str, Any] | None:
    prices = con.execute(
        "SELECT * FROM prices_daily WHERE symbol = ? ORDER BY date",
        [symbol],
    ).fetchdf()
    if len(prices) < 20:
        return None
    features = calculate_features(prices)
    con.execute(
        "INSERT OR REPLACE INTO technical_features (symbol, date, features) VALUES (?, ?, ?)",
        [symbol, features["date"], json_dumps(features)],
    )
    return features
