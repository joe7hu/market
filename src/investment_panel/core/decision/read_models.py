"""Decision read-model accessors."""

from __future__ import annotations
from typing import Any
from investment_panel.core.db import json_dumps, query_rows, upsert_instrument

from investment_panel.core.decision.coerce import decode
from investment_panel.core.decision.readiness import readiness_blockers, readiness_missing_inputs, readiness_next_action, readiness_portfolio_fit, readiness_status



def discovered_universe_rows(con: Any) -> list[dict[str, Any]]:
    return [decode(row) for row in query_rows(con, "SELECT * FROM discovered_universe ORDER BY universe_rank NULLS LAST, symbol LIMIT 1000")]




def decision_queue_rows(con: Any) -> list[dict[str, Any]]:
    return [decode(row) for row in query_rows(con, "SELECT * FROM decision_queue ORDER BY rank NULLS LAST, score DESC NULLS LAST LIMIT 250")]




def source_freshness_rows(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(con, "SELECT * FROM source_freshness ORDER BY docs_only ASC, checked_at DESC NULLS LAST, source_key")
    return [decode(row) for row in rows]




def symbol_decision_snapshot_rows(con: Any) -> list[dict[str, Any]]:
    return [decode(row) for row in query_rows(con, "SELECT * FROM symbol_decision_snapshots ORDER BY as_of DESC NULLS LAST, symbol LIMIT 250")]




def decision_readiness_rows(con: Any) -> list[dict[str, Any]]:
    """Decision-readiness contract for the app and API.

    This intentionally derives from the persisted decision queue so the UI can
    show both the underlying decision score and the action score after gates.
    """

    queue = [decode(row) for row in query_rows(con, "SELECT * FROM decision_queue ORDER BY rank ASC, action_score DESC NULLS LAST LIMIT 250")]
    portfolio_count = int(query_rows(con, "SELECT count(*) AS count FROM portfolio_positions")[0].get("count") or 0)
    output: list[dict[str, Any]] = []
    for row in queue:
        basis = row.get("decision_basis") if isinstance(row.get("decision_basis"), dict) else {}
        source_counts = basis.get("source_counts") if isinstance(basis.get("source_counts"), dict) else {}
        blockers = readiness_blockers(row, source_counts, portfolio_count)
        missing_inputs = readiness_missing_inputs(row, source_counts, portfolio_count)
        status = readiness_status(row, blockers, missing_inputs)
        output.append(
            {
                "symbol": row.get("symbol"),
                "status": status,
                "decision_score": row.get("decision_score"),
                "action_score": row.get("action_score"),
                "freshness_status": row.get("freshness_status"),
                "blockers": blockers,
                "missing_inputs": missing_inputs,
                "next_action": readiness_next_action(status, blockers, missing_inputs),
                "source_counts": source_counts,
                "portfolio_fit": readiness_portfolio_fit(row, portfolio_count),
                "as_of": row.get("as_of"),
            }
        )
    return output




def symbol_decision_snapshot(con: Any, symbol: str) -> dict[str, Any] | None:
    rows = query_rows(con, "SELECT * FROM symbol_decision_snapshots WHERE symbol = ? LIMIT 1", [symbol.upper()])
    return decode(rows[0]) if rows else None
