"""Bounded, replayable paper-order read model."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from investment_panel.database.runtime import DatabaseRuntime


class PaperOrderRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def rows(self, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 100))
        after = _decode_cursor(cursor)
        where = ""
        params: list[Any] = []
        if after is not None:
            where = "WHERE (paper.created_at, paper.id::text) < (%s, %s)"
            params.extend(after)
        with self.runtime.read() as connection:
            rows = connection.execute(
                f"""
                SELECT paper.id::text AS paper_order_id, paper.decision_id::text,
                       instrument.symbol, paper.created_at, paper.updated_at,
                       paper.side, paper.quantity, paper.limit_price, paper.status,
                       paper.lane, paper.policy_result, paper.policy_snapshot,
                       paper.ticket_version, paper.ticket_snapshot,
                       paper.submitted_at, paper.actual_fill_price, paper.filled_at,
                       paper.filled_quantity, paper.exited_quantity, paper.exit_price,
                       paper.exit_at, paper.fees, paper.entry_slippage,
                       paper.exit_slippage, paper.unfilled_reason
                FROM app.paper_order paper
                JOIN catalog.instrument instrument ON instrument.id = paper.instrument_id
                {where}
                ORDER BY paper.created_at DESC, paper.id DESC
                LIMIT %s
                """,
                [*params, bounded + 1],
            ).fetchall()
        values = [_payload(dict(row)) for row in rows]
        page = values[:bounded]
        return {
            "rows": page,
            "count": len(page),
            "limit": bounded,
            "next_cursor": _encode_cursor(page[-1]) if len(values) > bounded and page else None,
        }


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("created_at", "updated_at", "submitted_at", "filled_at", "exit_at"):
        if row.get(key) is not None:
            row[key] = row[key].isoformat()
    for key in (
        "quantity", "limit_price", "actual_fill_price", "filled_quantity",
        "exited_quantity", "exit_price", "fees", "entry_slippage", "exit_slippage",
    ):
        if row.get(key) is not None:
            row[key] = float(row[key])
    row["policy_result"] = dict(row.get("policy_result") or {})
    row["policy_snapshot"] = dict(row.get("policy_snapshot") or {})
    row["ticket_snapshot"] = dict(row.get("ticket_snapshot") or {})
    row["execution_status"] = _execution_status(row)
    return row


def _execution_status(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "").lower()
    if status in {"rejected", "unmeasurable"}:
        return "rejected"
    if status == "unfilled":
        return "unfilled"
    if status in {"exited", "invalidated"}:
        return "closed"
    if status in {"entered", "partial_exited"}:
        filled = float(row.get("filled_quantity") or row.get("quantity") or 0)
        exited = float(row.get("exited_quantity") or 0)
        return "partially_filled" if 0 < filled < float(row.get("quantity") or filled) or exited > 0 else "filled"
    return "submitted" if row.get("submitted_at") is not None else "staged"


def _encode_cursor(row: dict[str, Any]) -> str:
    payload = json.dumps([row["created_at"], row["paper_order_id"]], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        encoded = cursor + "=" * (-len(cursor) % 4)
        stamp, identifier = json.loads(base64.urlsafe_b64decode(encoded))
        value = datetime.fromisoformat(str(stamp))
        if value.tzinfo is None:
            raise ValueError
        return value, str(identifier)
    except Exception as exc:
        raise ValueError("invalid paper-order cursor") from exc
