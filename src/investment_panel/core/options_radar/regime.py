"""Market-regime context (QQQ trend, breadth) for gating and cohorts."""

from __future__ import annotations

import math
from typing import Any

from investment_panel.core.db import (query_rows)
from investment_panel.core.options_radar.coerce import (_average, _date, _number)

def _qqq_above_200d(con: Any, snapshot_time: str, cache: dict[str, bool | None]) -> bool | None:
    snapshot_date = _date(snapshot_time)
    if snapshot_date is None:
        return None
    key = snapshot_date.isoformat()
    if key in cache:
        return cache[key]
    rows = query_rows(
        con,
        """
        SELECT close
        FROM prices_daily
        WHERE symbol = 'QQQ' AND date <= TRY_CAST(? AS DATE)
        ORDER BY date DESC
        LIMIT 200
        """,
        [key],
    )
    closes = [_number(row.get("close")) for row in reversed(rows)]
    clean = [value for value in closes if value is not None]
    if len(clean) < 200:
        cache[key] = None
    else:
        cache[key] = clean[-1] >= (_average(clean[-200:]) or 10**9)
    return cache[key]


def _market_regime(con: Any, snapshot_time: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Two-dimensional market regime: {risk_on/neutral/risk_off} x {vol_low/vol_high}.

    Risk dimension from QQQ vs its 200d MA (distance buckets); vol dimension from the
    QQQ 20d realized-vol percentile over the trailing year. Replaces the binary
    QQQ-above-200d read for conditioning tail width and cohort base rates."""

    snapshot_date = _date(snapshot_time)
    if snapshot_date is None:
        return {"regime": "unknown", "risk": "unknown", "vol": "unknown"}
    key = snapshot_date.isoformat()
    if key in cache:
        return cache[key]
    rows = query_rows(
        con,
        "SELECT close FROM prices_daily WHERE symbol = 'QQQ' AND date <= TRY_CAST(? AS DATE) ORDER BY date DESC LIMIT 252",
        [key],
    )
    closes = [_number(r.get("close")) for r in reversed(rows)]
    clean = [c for c in closes if c is not None]
    if len(clean) < 200:
        result = {"regime": "unknown", "risk": "unknown", "vol": "unknown"}
        cache[key] = result
        return result
    ma200 = _average(clean[-200:]) or clean[-1]
    distance = clean[-1] / ma200 - 1.0 if ma200 else 0.0
    risk = "risk_on" if distance >= 0.02 else "risk_off" if distance <= -0.02 else "neutral"
    # Rolling 20d realized vol series -> current vol's percentile over the last ~year.
    rv_series: list[float] = []
    for end in range(21, len(clean) + 1):
        window = clean[end - 21 : end]
        rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window)) if window[i - 1] > 0]
        if len(rets) >= 2:
            avg = sum(rets) / len(rets)
            var = sum((r - avg) ** 2 for r in rets) / (len(rets) - 1)
            rv_series.append(math.sqrt(var) * math.sqrt(252))
    vol = "unknown"
    if rv_series:
        current = rv_series[-1]
        pct = sum(1 for v in rv_series if v <= current) / len(rv_series)
        vol = "vol_high" if pct >= 0.5 else "vol_low"
    result = {"regime": f"{risk}/{vol}", "risk": risk, "vol": vol, "qqq_distance_200d": round(distance, 4)}
    cache[key] = result
    return result
