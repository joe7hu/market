"""Correlation discovery over stored price history."""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from investment_panel.core.db import json_dumps


def store_correlation_runs(con: Any, symbols: list[str], lookback_days: int = 180, max_peers: int = 8) -> int:
    if len(symbols) < 2:
        return 0
    prices = con.execute(
        """
        SELECT symbol, date, close
        FROM prices_daily
        WHERE symbol IN ({})
        ORDER BY date
        """.format(",".join("?" for _ in symbols)),
        symbols,
    ).fetchdf()
    if prices.empty:
        return 0
    pivot = prices.pivot_table(index="date", columns="symbol", values="close").tail(lookback_days)
    if pivot.shape[1] < 2:
        return 0
    returns = np.log(pivot / pivot.shift(1)).dropna(how="all")
    corr = returns.corr(min_periods=max(20, min(60, len(returns) // 2)))
    today = date.today().isoformat()
    count = 0
    for symbol in [item for item in symbols if item in corr.columns]:
        peers = []
        series = corr[symbol].drop(labels=[symbol], errors="ignore").dropna()
        for peer_symbol, value in series.reindex(series.abs().sort_values(ascending=False).index).head(max_peers).items():
            peers.append({"symbol": peer_symbol, "correlation": round(float(value), 4)})
        run_id = stable_id(f"{today}:{symbol}:{lookback_days}")
        con.execute(
            """
            INSERT OR REPLACE INTO correlation_runs
            (id, target_symbol, as_of, lookback_days, peers, metrics)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                symbol,
                today,
                lookback_days,
                json_dumps(peers),
                json_dumps({"observations": len(returns), "universe_size": pivot.shape[1]}),
            ],
        )
        count += 1
    return count


def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
