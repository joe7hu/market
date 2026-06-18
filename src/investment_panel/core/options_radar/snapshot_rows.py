"""Shared option_snapshot row persistence."""

from __future__ import annotations

from typing import Any

from investment_panel.core.db import json_dumps


def insert_option_snapshot_row(con: Any, row: dict[str, Any]) -> None:
    """Persist one normalized option_snapshot row."""

    con.execute(
        """
        INSERT OR REPLACE INTO option_snapshot
        (snapshot_time, ticker, underlying_price, expiration, strike, option_type, bid, ask, mid,
         last, volume, open_interest, iv, delta, gamma, theta, vega, dte, spread_pct,
         data_source, contract_id, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            row.get("snapshot_time"),
            row.get("ticker"),
            row.get("underlying_price"),
            row.get("expiration"),
            row.get("strike"),
            row.get("option_type"),
            row.get("bid"),
            row.get("ask"),
            row.get("mid"),
            row.get("last"),
            row.get("volume"),
            row.get("open_interest"),
            row.get("iv"),
            row.get("delta"),
            row.get("gamma"),
            row.get("theta"),
            row.get("vega"),
            row.get("dte"),
            row.get("spread_pct"),
            row.get("data_source"),
            row.get("contract_id"),
            json_dumps(row.get("raw") or {}),
        ],
    )
