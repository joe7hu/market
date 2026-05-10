"""Portfolio import and review helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


def import_portfolio_csv(con: Any, csv_path: Path | None) -> int:
    if csv_path is None or not csv_path.exists():
        return 0
    frame = pd.read_csv(csv_path)
    normalized = frame.rename(columns={column: column.strip().lower() for column in frame.columns})
    column_map = {
        "ticker": "symbol",
        "shares": "quantity",
        "cost_basis": "avg_cost",
        "note": "notes",
    }
    normalized = normalized.rename(columns={key: value for key, value in column_map.items() if key in normalized.columns})
    for required in ("symbol", "quantity", "avg_cost"):
        if required not in normalized.columns:
            normalized[required] = 0.0 if required != "symbol" else ""
    if "notes" not in normalized.columns:
        normalized["notes"] = ""
    normalized["symbol"] = normalized["symbol"].astype(str).str.upper()
    con.register("portfolio_frame", normalized[["symbol", "quantity", "avg_cost", "notes"]])
    con.execute(
        """
        INSERT OR REPLACE INTO portfolio_positions
        SELECT symbol, quantity::DOUBLE, avg_cost::DOUBLE, notes
        FROM portfolio_frame
        WHERE symbol != ''
        """
    )
    con.unregister("portfolio_frame")
    return len(normalized)


def seed_empty_theses_for_portfolio(con: Any) -> int:
    rows = con.execute("SELECT symbol FROM portfolio_positions").fetchall()
    count = 0
    for (symbol,) in rows:
        con.execute(
            """
            INSERT OR IGNORE INTO theses (symbol, thesis_json, updated_at)
            VALUES (?, ?, now())
            """,
            [
                symbol,
                '{"position_status":"owned","core_thesis":"","pillars":[],"risks":[],"invalidation":[],"catalysts":[],"conviction":"unknown"}',
            ],
        )
        count += 1
    return count

