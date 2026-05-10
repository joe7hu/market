"""SEPA-style momentum setup analysis from stored OHLCV."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from investment_panel.core.db import json_dumps


def analyze_sepa(prices: pd.DataFrame) -> dict[str, Any] | None:
    if len(prices) < 50:
        return None
    ordered = prices.sort_values("date").copy()
    close = ordered["close"].astype(float)
    volume = ordered["volume"].astype(float)
    current = float(close.iloc[-1])
    ma50 = float(close.rolling(50).mean().iloc[-1])
    ma150 = float(close.rolling(min(150, len(close))).mean().iloc[-1])
    ma200_series = close.rolling(min(200, len(close))).mean()
    ma200 = float(ma200_series.iloc[-1])
    ma200_1m = float(ma200_series.iloc[-min(22, len(ma200_series))])
    high_52 = float(close.rolling(min(252, len(close))).max().iloc[-1])
    low_52 = float(close.rolling(min(252, len(close))).min().iloc[-1])
    avg_volume_20 = float(volume.tail(20).mean())
    current_volume = float(volume.iloc[-1])
    checklist = {
        "price_above_150ma_and_200ma": current > ma150 and current > ma200,
        "ma150_above_ma200": ma150 > ma200,
        "ma200_rising_1m": ma200 > ma200_1m,
        "ma50_above_150ma_and_200ma": ma50 > ma150 and ma50 > ma200,
        "price_above_50ma": current > ma50,
        "price_30pct_above_52w_low": current >= low_52 * 1.30,
        "price_within_25pct_of_52w_high": current >= high_52 * 0.75,
    }
    passes = sum(1 for passed in checklist.values() if passed)
    stage = stage_for(current, ma50, ma150, ma200, checklist)
    volume_ratio = current_volume / max(avg_volume_20, 1)
    score = round((passes / len(checklist)) * 82 + min(volume_ratio, 2.0) * 9, 2)
    verdict = "strong_setup" if score >= 82 and stage == "stage_2_advancing" else "watch" if score >= 62 else "pass"
    return {
        "score": min(score, 100.0),
        "stage": stage,
        "verdict": verdict,
        "checklist": checklist,
        "metrics": {
            "close": current,
            "ma50": ma50,
            "ma150": ma150,
            "ma200": ma200,
            "ma200_1m_ago": ma200_1m,
            "high_52w": high_52,
            "low_52w": low_52,
            "avg_volume_20": avg_volume_20,
            "volume_ratio": volume_ratio,
            "conditions_passed": passes,
            "conditions_total": len(checklist),
        },
    }


def stage_for(current: float, ma50: float, ma150: float, ma200: float, checklist: dict[str, bool]) -> str:
    if current > ma50 > ma150 > ma200 and checklist["ma200_rising_1m"]:
        return "stage_2_advancing"
    if current < ma50 and ma50 < ma150 and ma150 < ma200:
        return "stage_4_declining"
    if current >= ma200 * 0.9 and current <= ma200 * 1.15:
        return "stage_1_basing"
    return "stage_3_or_transition"


def store_sepa_analyses(con: Any, symbols: list[str]) -> int:
    today = date.today().isoformat()
    count = 0
    for symbol in symbols:
        prices = con.execute("SELECT * FROM prices_daily WHERE symbol = ? ORDER BY date", [symbol]).fetchdf()
        analysis = analyze_sepa(prices)
        if not analysis:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO sepa_analyses
            (symbol, as_of, score, stage, verdict, checklist, metrics)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                symbol,
                today,
                analysis["score"],
                analysis["stage"],
                analysis["verdict"],
                json_dumps(analysis["checklist"]),
                json_dumps(analysis["metrics"]),
            ],
        )
        count += 1
    return count
