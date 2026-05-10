"""Liquidity metrics from stored OHLCV and latest quotes."""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np

from investment_panel.core.db import json_dumps


def store_liquidity_metrics(con: Any, symbols: list[str]) -> int:
    today = date.today().isoformat()
    count = 0
    for symbol in symbols:
        prices = con.execute("SELECT * FROM prices_daily WHERE symbol = ? ORDER BY date", [symbol]).fetchdf()
        if len(prices) < 20:
            continue
        close = prices["close"].astype(float)
        volume = prices["volume"].astype(float)
        returns = close.pct_change().dropna()
        dollar_volume = close * volume
        avg_volume = float(volume.tail(60).mean())
        avg_dollar_volume = float(dollar_volume.tail(60).mean())
        aligned_dollar_volume = dollar_volume.iloc[1:]
        amihud_series = returns.abs() / aligned_dollar_volume.replace(0, np.nan)
        amihud = float(amihud_series.replace([np.inf, -np.inf], np.nan).dropna().mean() or 0)
        daily_volatility = float(returns.tail(60).std() or 0)
        impact_bps = daily_volatility * np.sqrt(0.01) * 10000
        grade = liquidity_grade(avg_dollar_volume, amihud * 1e9)
        metrics = {
            "last_close": float(close.iloc[-1]),
            "avg_daily_volume_60d": avg_volume,
            "avg_dollar_volume_60d": avg_dollar_volume,
            "amihud_illiquidity_x1e9": amihud * 1e9,
            "daily_volatility_60d": daily_volatility,
            "impact_1pct_adv_bps": impact_bps,
            "observations": len(prices),
        }
        con.execute(
            """
            INSERT OR REPLACE INTO liquidity_metrics
            (symbol, as_of, grade, avg_daily_volume, avg_dollar_volume, turnover_ratio,
             amihud_illiquidity, impact_1pct_adv_bps, metrics)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [symbol, today, grade, avg_volume, avg_dollar_volume, None, amihud, impact_bps, json_dumps(metrics)],
        )
        count += 1
    return count


def liquidity_grade(avg_dollar_volume: float, amihud_x1e9: float) -> str:
    if avg_dollar_volume > 500_000_000 and amihud_x1e9 < 0.01:
        return "very_high"
    if avg_dollar_volume > 50_000_000 and amihud_x1e9 < 0.1:
        return "high"
    if avg_dollar_volume > 5_000_000 and amihud_x1e9 < 1.0:
        return "moderate"
    if avg_dollar_volume > 500_000 and amihud_x1e9 < 10.0:
        return "low"
    return "very_low"
