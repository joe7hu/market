"""Portfolio import and review helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from investment_panel.core.instruments import infer_asset_class, normalize_symbol


def portfolio_instruments(con: Any) -> list[dict[str, Any]]:
    """Return owned symbols as instrument rows without overwriting richer metadata."""

    rows = con.execute(
        """
        SELECT p.symbol, i.name, i.asset_class, i.sector, i.industry, i.category, i.source
        FROM portfolio_positions p
        LEFT JOIN instruments i ON i.symbol = p.symbol
        ORDER BY p.symbol
        """
    ).fetchall()
    output: list[dict[str, Any]] = []
    for symbol, name, asset_class, sector, industry, category, source in rows:
        normalized = normalize_symbol(str(symbol or ""))
        if not normalized:
            continue
        output.append(
            {
                "symbol": normalized,
                "name": name or normalized,
                "asset_class": asset_class or infer_asset_class(normalized),
                "sector": sector,
                "industry": industry,
                "category": category or "owned-portfolio",
                "source": source or "portfolio",
                "cik": None,
            }
        )
    return output


def ensure_portfolio_instruments(con: Any) -> int:
    """Make manual/CSV portfolio symbols first-class instruments for refresh jobs."""

    rows = portfolio_instruments(con)
    for row in rows:
        con.execute(
            """
            INSERT INTO instruments (symbol, name, asset_class, sector, industry, category, source)
            SELECT ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (SELECT 1 FROM instruments WHERE symbol = ?)
            """,
            [
                row["symbol"],
                row.get("name"),
                row.get("asset_class"),
                row.get("sector"),
                row.get("industry"),
                row.get("category"),
                row.get("source"),
                row["symbol"],
            ],
        )
        con.execute(
            """
            UPDATE instruments
            SET name = COALESCE(NULLIF(name, ''), ?),
                asset_class = COALESCE(NULLIF(asset_class, ''), ?),
                category = COALESCE(NULLIF(category, ''), ?),
                source = COALESCE(NULLIF(source, ''), ?)
            WHERE symbol = ?
            """,
            [row.get("name"), row.get("asset_class"), row.get("category"), row.get("source"), row["symbol"]],
        )
    return len(rows)


def import_portfolio_csv(con: Any, csv_path: Path | None) -> int:
    if csv_path is None or not csv_path.exists():
        return 0
    frame = pd.read_csv(csv_path)
    normalized = frame.rename(columns={column: column.strip().lower() for column in frame.columns})
    column_map = {
        "ticker": "symbol",
        "shares": "quantity",
        "cost_basis": "avg_cost",
        "average_cost": "avg_cost",
        "avg price": "avg_cost",
        "purchase day": "purchase_date",
        "purchase_date": "purchase_date",
        "purchase date": "purchase_date",
        "acquired_date": "purchase_date",
        "acquired date": "purchase_date",
        "note": "notes",
    }
    normalized = normalized.rename(columns={key: value for key, value in column_map.items() if key in normalized.columns})
    for required in ("symbol", "quantity", "avg_cost"):
        if required not in normalized.columns:
            normalized[required] = 0.0 if required != "symbol" else ""
    if "purchase_date" not in normalized.columns:
        normalized["purchase_date"] = None
    if "notes" not in normalized.columns:
        normalized["notes"] = ""
    normalized["symbol"] = normalized["symbol"].astype(str).str.upper()
    normalized["purchase_date"] = pd.to_datetime(normalized["purchase_date"], errors="coerce").dt.date
    con.register("portfolio_frame", normalized[["symbol", "quantity", "avg_cost", "purchase_date", "notes"]])
    con.execute(
        """
        INSERT OR REPLACE INTO portfolio_positions (symbol, quantity, avg_cost, purchase_date, notes)
        SELECT symbol, quantity::DOUBLE, avg_cost::DOUBLE, purchase_date::DATE, notes
        FROM portfolio_frame
        WHERE symbol != ''
        """
    )
    con.unregister("portfolio_frame")
    ensure_portfolio_instruments(con)
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
