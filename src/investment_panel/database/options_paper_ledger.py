"""Shared paper-options sleeve gates used by every execution lane."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from investment_panel.core.decision import MARKET_TZ


OPEN_STATUSES = ("staged", "open", "entered", "partial_exited")


def shared_sleeve_blockers(
    connection: Any,
    *,
    now: datetime,
    lane: str,
    sleeve_capital: float | None,
    daily_loss_halt_pct: float | None,
    max_open_positions: int | None,
) -> list[str]:
    """Return deterministic cross-lane capacity blockers.

    This is deliberately independent of a lane's quote and calibration gates.
    Each lane calls it while holding the same PostgreSQL advisory transaction
    lock, so Radar, QQQ, and Recovery cannot allocate the same sleeve twice.
    """

    blockers: list[str] = []
    if lane not in {"radar", "qqq", "recovery"}:
        return ["unknown_options_paper_lane"]
    if sleeve_capital is None or sleeve_capital <= 0:
        return ["options_risk_sleeve_required"]
    day_start = now.astimezone(MARKET_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ).astimezone(UTC)
    row = connection.execute(
        """
        SELECT
          count(*) FILTER (WHERE status = ANY(%s)) AS open_positions,
          count(*) FILTER (WHERE lane = %s AND created_at >= %s) AS lane_new_today,
          count(*) FILTER (WHERE lane = %s AND status = 'exited') AS clean_completed_lifecycles
        FROM app.paper_order
        """,
        [list(OPEN_STATUSES), lane, day_start, lane],
    ).fetchone()
    # A production aggregate always returns one row.  Treat an absent row as
    # an empty ledger so lightweight repository adapters remain fail-safe for
    # capacity (rather than throwing before their own transaction can reject).
    summary = dict(row or {})
    if max_open_positions is not None and max_open_positions > 0 and int(summary.get("open_positions") or 0) >= max_open_positions:
        blockers.append("shared_open_position_cap_reached")
    # A lane stays in its cautious one-new-position phase until it has five
    # complete, non-invalidated paper lifecycles.  This is the same guard for
    # each lane; another lane's success cannot accelerate it.
    if max_open_positions is not None and int(summary.get("clean_completed_lifecycles") or 0) < 5 and int(summary.get("lane_new_today") or 0) >= 1:
        blockers.append("lane_initial_one_new_position_per_day")

    if daily_loss_halt_pct is not None and daily_loss_halt_pct > 0:
        pnl = connection.execute(
            """
            SELECT coalesce(sum((details->>'net_pnl')::numeric), 0) AS value
            FROM app.trade_journal
            WHERE created_at >= %s AND created_at <= %s
              AND details ? 'net_pnl'
              AND (details->>'net_pnl') ~ '^-?[0-9]+(\\.[0-9]+)?$'
            """,
            [day_start, now],
        ).fetchone()
        loss_halt = sleeve_capital * daily_loss_halt_pct
        if float((pnl or {}).get("value") or 0) <= -loss_halt:
            blockers.append("shared_daily_loss_halt")
    return blockers


def acquire_shared_sleeve_lock(connection: Any) -> None:
    """Serialize capacity checks across all options paper lanes."""

    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        ["paper-order:shared-options-sleeve"],
    )
