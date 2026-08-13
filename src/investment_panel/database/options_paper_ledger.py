"""Shared paper-options sleeve gates used by every execution lane."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from investment_panel.core.decision import MARKET_TZ


OPEN_STATUSES = ("staged", "open", "entered", "partial_exited")


def active_paper_exposure(
    connection: Any,
    *,
    symbol: str,
    instrument_id: int,
) -> dict[str, Any]:
    """Return the risk and collateral reserved by every still-open paper order.

    A partial exit releases only the exited fraction.  The order's immutable
    ticket keeps its original quantity and total risk, so all aggregates must
    scale that amount by the remaining filled quantity rather than counting the
    full original ticket or dropping the order entirely.
    """

    row = connection.execute(
        """
        WITH active_order AS (
          SELECT paper_order.*, instrument.symbol,
                 CASE
                   WHEN paper_order.status IN ('staged', 'open')
                     THEN paper_order.quantity
                   WHEN paper_order.status IN ('entered', 'partial_exited')
                     THEN greatest(
                       coalesce(paper_order.filled_quantity, paper_order.quantity)
                       - coalesce(paper_order.exited_quantity, 0),
                       0
                     )
                   ELSE 0
                 END AS remaining_quantity,
                 CASE
                   WHEN paper_order.structure = 'cash_secured_put'
                     THEN paper_order.reserved_collateral
                   ELSE coalesce(
                     (paper_order.ticket_snapshot->'risk'->>'total_risk')::numeric,
                     paper_order.quantity * option_decision.max_loss,
                     paper_order.reserved_collateral,
                     abs(paper_order.quantity * paper_order.limit_price)
                   )
                 END AS full_commitment
          FROM app.paper_order paper_order
          JOIN catalog.instrument instrument ON instrument.id = paper_order.instrument_id
          LEFT JOIN analysis.decision decision ON decision.id = paper_order.decision_id
          LEFT JOIN analysis.option_decision option_decision
            ON option_decision.decision_id = decision.id
          WHERE paper_order.status = ANY(%s)
        ), valued_order AS (
          SELECT *,
                 CASE
                   WHEN full_commitment IS NOT NULL
                    AND full_commitment > 0
                    AND full_commitment <> 'NaN'::numeric
                   THEN full_commitment * remaining_quantity / nullif(quantity, 0)
                 END AS remaining_commitment,
                 CASE
                   WHEN reserved_collateral IS NOT NULL
                    AND reserved_collateral > 0
                    AND reserved_collateral <> 'NaN'::numeric
                   THEN reserved_collateral * remaining_quantity / nullif(quantity, 0)
                 END AS remaining_csp_collateral
          FROM active_order
          WHERE remaining_quantity > 0
        )
        SELECT
          coalesce(sum(
            CASE WHEN coalesce(structure, '') <> 'cash_secured_put' AND symbol = %s
              THEN remaining_commitment ELSE 0 END
          ), 0) AS symbol_risk,
          coalesce(sum(
            CASE WHEN coalesce(structure, '') <> 'cash_secured_put'
              THEN remaining_commitment ELSE 0 END
          ), 0) AS total_risk,
          coalesce(sum(remaining_commitment), 0) AS total_committed,
          count(*) FILTER (
            WHERE remaining_commitment IS NULL
               OR (structure = 'cash_secured_put' AND remaining_csp_collateral IS NULL)
          ) AS unvalued_commitments,
          coalesce(sum(
            CASE WHEN structure = 'cash_secured_put'
              THEN remaining_csp_collateral ELSE 0 END
          ), 0) AS total_csp_collateral,
          coalesce(sum(
            CASE WHEN structure = 'cash_secured_put' AND instrument_id = %s
              THEN remaining_csp_collateral ELSE 0 END
          ), 0) AS symbol_csp_collateral
        FROM valued_order
        """,
        [list(OPEN_STATUSES), symbol, instrument_id],
    ).fetchone()
    return dict(row or {})


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
