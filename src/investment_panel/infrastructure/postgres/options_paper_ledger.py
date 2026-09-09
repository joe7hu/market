"""Shared paper-options sleeve gates used by every execution lane."""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from typing import Any

from investment_panel.domain.decision import MARKET_TZ


OPEN_STATUSES = ("staged", "open", "entered", "partial_exited")

# Used by execution, loss reconciliation, learning, and stored paper proof.
# Each cash flow must preserve its observed multiplier; an order/catalog
# snapshot cannot supply missing evidence for an earlier journal entry.
PAPER_FILL_MULTIPLIERS_SQL = """bool_and(coalesce(
    paper.contract_multiplier > 0 AND paper.contract_multiplier < 'Infinity'::numeric
    AND CASE WHEN action = 'paper_entry' THEN
        jsonb_typeof(details->'contract_multiplier') = 'number'
        AND details->'contract_multiplier' = to_jsonb(paper.contract_multiplier)
    ELSE
        jsonb_typeof(details->'entry_contract_multiplier') = 'number'
        AND jsonb_typeof(details->'exit_contract_multiplier') = 'number'
        AND details->'entry_contract_multiplier' = to_jsonb(paper.contract_multiplier)
        AND details->'exit_contract_multiplier' = to_jsonb(paper.contract_multiplier)
    END, false))"""


def paper_fill_totals(connection: Any, order: dict[str, Any], *, as_of: datetime) -> dict[str, Any] | None:
    """Read only this order's journal cash flows, including prior partial exits."""
    return connection.execute(
        f"""SELECT coalesce(sum(quantity) FILTER (WHERE action = 'paper_entry'), 0) AS entry_quantity,
                  coalesce(sum(quantity) FILTER (WHERE action <> 'paper_entry'), 0) AS exit_quantity,
                  coalesce(sum(quantity * price) FILTER (WHERE action = 'paper_entry'), 0) AS entry_units,
                  coalesce(sum(quantity * price) FILTER (WHERE action <> 'paper_entry'), 0) AS exit_units,
                  coalesce(sum((details->>'fees')::numeric)
                      FILTER (WHERE details->>'fees' ~ '^[0-9]+([.][0-9]+)?$'), 0) AS actual_fees,
                  coalesce(sum((details->>'fees')::numeric)
                      FILTER (WHERE action = 'paper_entry' AND details->>'fees' ~ '^[0-9]+([.][0-9]+)?$'), 0) AS entry_fees,
                  count(*) FILTER (WHERE NOT coalesce(details->>'fees' ~ '^[0-9]+([.][0-9]+)?$', false)) AS missing_fees,
                  count(*) FILTER (WHERE NOT coalesce(
                      quantity > 0 AND quantity < 'Infinity'::numeric
                      AND price >= 0 AND price < 'Infinity'::numeric
                      AND (action <> 'paper_entry' OR price > 0), false)) AS invalid_fills,
                  {PAPER_FILL_MULTIPLIERS_SQL} AS fill_multipliers_verified,
                  array_agg(id::text ORDER BY created_at, id) AS journal_ids,
                  array_agg(id::text ORDER BY created_at, id) FILTER (WHERE action = 'paper_entry') AS entry_journal_ids
           FROM app.trade_journal
           CROSS JOIN (SELECT %s::numeric AS contract_multiplier) paper
           WHERE details->>'paper_order_id' = %s AND decision_id = %s::uuid
             AND (action IN ('paper_entry', 'paper_exit') OR action LIKE 'paper_exit:%%')
             AND created_at <= %s AND rationale = 'deterministic_options_paper_execution'""",
        [order.get("contract_multiplier"), str(order["id"]), order.get("decision_id"), as_of],
    ).fetchone()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _reconciled_exit_pnl(row: dict[str, Any], fills: dict[str, Any] | None) -> float | None:
    """Recheck an unknown exit from the same fill and paid-fee evidence."""
    if not fills or fills["missing_fees"] or fills["invalid_fills"] or fills.get("fill_multipliers_verified") is not True:
        return None
    if str(row["journal_id"]) not in (fills.get("journal_ids") or []):
        return None
    values = {name: _number(fills[name]) for name in ("entry_quantity", "exit_quantity", "entry_units", "entry_fees", "actual_fees")}
    filled, exited = _number(row.get("filled_quantity")), _number(row.get("exited_quantity"))
    entry_fees, paid_fees = _number(row.get("entry_fees")), _number(row.get("fees"))
    multiplier = _number(row.get("contract_multiplier"))
    details = row["journal_details"] or {}
    entry_multiplier = _number(details.get("entry_contract_multiplier"))
    exit_multiplier = _number(details.get("exit_contract_multiplier"))
    # Missing legacy evidence and recorded conflicts remain unknown. The
    # current catalog/order multiplier cannot repair the two exit-time facts.
    if entry_multiplier is None or exit_multiplier is None or not 0 < entry_multiplier == exit_multiplier == multiplier:
        return None
    quantity, price = _number(row["journal_quantity"]), _number(row["journal_price"])
    exit_fees = _number(details.get("fees"))
    if any(value is None for value in (*values.values(), filled, exited, entry_fees, paid_fees, multiplier, quantity, price, exit_fees)):
        return None
    if (filled <= 0 or values["entry_quantity"] != filled or values["exit_quantity"] != exited
        or values["entry_units"] <= 0 or not 0 < quantity <= exited <= filled
        or multiplier <= 0 or price < 0 or exit_fees < 0 or entry_fees < 0 or paid_fees < 0
        or abs(values["entry_fees"] - entry_fees) > 1e-6 or abs(values["actual_fees"] - paid_fees) > 1e-6):
        return None
    structure = row.get("structure") or (row.get("ticket_snapshot") or {}).get("structure")
    credit = structure in {"cash_secured_put", "put_credit_spread", "call_credit_spread"}
    if not credit and structure not in {"long_call", "long_put", "call_debit_spread", "put_debit_spread"}:
        return None
    entry_vwap = values["entry_units"] / filled
    gross = (entry_vwap - price if credit else price - entry_vwap) * multiplier * quantity
    result = gross - entry_fees * quantity / filled - exit_fees
    return round(result, 2) if isfinite(result) else None


def shared_sleeve_loss_state(connection: Any, *, now: datetime) -> dict[str, Any]:
    """Keep unresolved exit accounting visible until its journals reconcile.

    Reconciliation is read-only. The original unknown exit remains in the
    audit journal; restored fill/fee evidence is checked again at each cutoff.
    Known P&L belongs to the exit journal's New York day. Unknown exits remain
    unresolved across day boundaries instead of becoming zero at midnight.
    """
    day_start = now.astimezone(MARKET_TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    rows = connection.execute(
        """SELECT paper.*, journal.id::text AS journal_id, journal.action AS journal_action,
                  journal.created_at AS journal_at, journal.quantity AS journal_quantity,
                  journal.price AS journal_price, journal.details AS journal_details
           FROM app.trade_journal journal
           LEFT JOIN app.paper_order paper ON paper.id::text = journal.details->>'paper_order_id'
                AND paper.decision_id = journal.decision_id AND paper.instrument_id = journal.instrument_id
                AND paper.paper_only IS TRUE AND paper.created_at <= %s
           WHERE journal.created_at <= %s AND (
               journal.action = 'paper_exit' OR journal.action LIKE 'paper_exit:%%'
               OR (journal.created_at >= %s AND journal.details ? 'net_pnl'))
             AND (journal.created_at >= %s OR NOT coalesce(
                 journal.details->>'net_pnl' ~ '^-?[0-9]{1,18}([.][0-9]{1,8})?$', false))
           ORDER BY journal.created_at, journal.id""",
        [now, now, day_start, day_start],
    ).fetchall()
    total, unresolved, reconciled = 0.0, 0, 0
    fills_by_order: dict[Any, Any] = {}
    for source in rows:
        row = dict(source)
        value = _number((row["journal_details"] or {}).get("net_pnl"))
        if value is None and row.get("id") is not None and row.get("updated_at") <= now:
            order_id = row["id"]
            if order_id not in fills_by_order:
                fills_by_order[order_id] = paper_fill_totals(connection, row, as_of=now)
            value = _reconciled_exit_pnl(row, fills_by_order[order_id])
            reconciled += int(value is not None)
        if value is None:
            unresolved += 1
        elif row["journal_at"] >= day_start:
            total += value
    if not isfinite(total):
        unresolved += 1
    return {"value": total if not unresolved else None, "unresolved_exits": unresolved, "reconciled_exits": reconciled}


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
        WHERE created_at <= %s
        """,
        [list(OPEN_STATUSES), lane, day_start, lane, now],
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

    pnl = shared_sleeve_loss_state(connection, now=now)
    if pnl["unresolved_exits"]:
        blockers.append("shared_exit_accounting_unreconciled")
    if daily_loss_halt_pct is not None and daily_loss_halt_pct > 0:
        loss_halt = sleeve_capital * daily_loss_halt_pct
        if pnl["value"] is not None and pnl["value"] <= -loss_halt:
            blockers.append("shared_daily_loss_halt")
    return blockers


def acquire_shared_sleeve_lock(connection: Any) -> None:
    """Serialize capacity checks across all options paper lanes."""

    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        ["paper-order:shared-options-sleeve"],
    )
