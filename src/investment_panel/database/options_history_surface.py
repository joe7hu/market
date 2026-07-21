"""Compact, bounded expiry/type selectors for option-history evidence."""

from __future__ import annotations

from typing import Any

from investment_panel.database.runtime import DatabaseRuntime


def surface_groups(runtime: DatabaseRuntime, snapshot_id: int) -> list[dict[str, Any]]:
    with runtime.read() as connection:
        rows = connection.execute(
            """
            SELECT contract.expiration, contract.option_type,
                   greatest(contract.expiration - snapshot.trading_date, 0) AS dte, count(*) AS contract_count
            FROM raw.option_quote quote
            JOIN catalog.option_contract contract ON contract.id = quote.contract_id
            JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
            WHERE quote.snapshot_id = %s AND quote.capture_generation_id = snapshot.latest_complete_generation_id
            GROUP BY contract.expiration, contract.option_type, snapshot.trading_date
            ORDER BY contract.expiration, contract.option_type
            """, [snapshot_id],
        ).fetchall()
    return [dict(row) for row in rows]
