"""Causal experiment event journal; never an account fill or funded NAV input."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from math import isfinite
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.domain.decision import is_market_open, market_session_elapsed
from investment_panel.infrastructure.postgres.runtime import JOB_PROFILE


def record_experiment_event(
    connection: Any, *, shadow_id: Any, kind: str, observed_at: datetime,
    quotes: list[dict[str, Any]], price: float | None = None,
    net_pnl: float | None = None, net_return: float | None = None,
    fees: float | None = None, reason: str | None = None,
) -> None:
    """Write in the same transaction as the position; repeated quotes are inert.

    Marks use actual entry ask and liquidation bid, plus both sides' fees.
    Quotes are already validated by the execution owner. Retain its witness,
    including quote identifiers and source clocks, instead of repricing later.
    """
    if kind not in {"entry", "mark", "exit", "mark_gap"}:
        raise ValueError("unsupported experiment event")
    if _time(observed_at) is None:
        raise ValueError("experiment event clock must be timezone aware")
    clocks = [_time(quote.get("observed_at")) for quote in quotes]
    quote_at = min((clock for clock in clocks if clock is not None), default=None)
    if kind != "mark_gap":
        if not quotes or any(clock is None or clock > observed_at for clock in clocks):
            raise ValueError("experiment event requires causal observed quotes")
        if any(value is None or not isfinite(float(value)) for value in (price, net_pnl, net_return, fees)):
            raise ValueError("experiment event requires finite accounting")
    elif not reason or any(value is not None for value in (price, net_pnl, net_return, fees)):
        raise ValueError("a mark gap is not a zero return")
    evidence = {"quotes": quotes, "quantity": 1, "multiplier": 100,
                "basis": "one_contract_liquidation_after_entry_and_exit_fees",
                "execution_basis": "later_complete_quote_worst_side"}
    # Same quote cannot create 60 new observations from 60 worker ticks.
    # Availability/ingestion time and database IDs can change on a recapture
    # of the same provider tick. They are evidence, not a new mark identity.
    witnesses = [{key: quote.get(key) for key in (
        "contract_id", "symbol", "expiration", "strike", "option_type", "source_id", "observed_at"
    )} for quote in quotes]
    for witness in witnesses:
        clock = _time(witness.get("observed_at"))
        witness["observed_at"] = clock.isoformat() if clock else None
    identity = {"kind": kind, "quotes": sorted(witnesses, key=lambda item: json.dumps(item, default=str, sort_keys=True)), "reason": reason}
    if kind == "mark_gap":
        identity["bucket"] = int(observed_at.timestamp()) // 300
    key = hashlib.sha256(json.dumps(identity, default=str, sort_keys=True).encode()).hexdigest()
    connection.execute(
        """INSERT INTO analysis.option_experiment_event
               (shadow_trade_id, event_key, kind, observed_at, quote_observed_at,
                price, net_pnl, net_return, fees, reason, evidence)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (shadow_trade_id, event_key) DO NOTHING""",
        [shadow_id, key, kind, observed_at, quote_at, price, net_pnl, net_return,
         fees, reason, Jsonb(evidence, dumps=lambda value: json.dumps(value, default=str))],
    )


def observation_lifecycle(row: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Separate the current holding task from its frozen admission checklist."""
    result = dict(row)
    state = str(row.get("status") or "")
    ticket_action = row.get("required_next_action")
    result["admission_next_action"] = ticket_action
    result["quantity"], result["multiplier"] = 1, 100
    entry = _number(row.get("entry_price"))
    net_return = _number(row.get("net_return"))
    result["net_pnl"] = entry * 100 * net_return if entry is not None and entry > 0 and net_return is not None else None
    marked_at = _time(row.get("quote_observed_at"))
    if marked_at is None:
        clocks = [_time(quote.get("observed_at")) for quote in row.get("observed_quotes") or [] if isinstance(quote, dict)]
        marked_at = min((clock for clock in clocks if clock is not None), default=None)
    result["quote_observed_at"] = marked_at
    result["mark_age_seconds"] = max(0.0, (now - marked_at).total_seconds()) if marked_at else None
    if state == "entered":
        elapsed = market_session_elapsed(marked_at, now).total_seconds() if marked_at and marked_at <= now else None
        overdue = elapsed is None or elapsed > 300
        result["mark_status"] = "overdue" if overdue else "last_session" if not is_market_open(now) else "current"
        result["reason"] = "mark_refresh_overdue" if result["mark_status"] == "overdue" else "position_open"
        result["required_next_action"] = (
            "The paper worker must obtain a fresh executable quote; the displayed P&L is the last recorded mark, not a current valuation."
            if result["mark_status"] == "overdue" else
            "The paper worker is managing the frozen stop, profit target and time exit. No new entry instruction applies."
        )
    elif state == "closed":
        result["reason"] = row.get("exit_reason") or "position_closed"
        result["mark_status"] = "realized"
        result["required_next_action"] = "The outcome evaluator measures this completed observation; promotion still requires independent evidence."
    elif state == "unfilled":
        result["required_next_action"] = "Entry window ended without a qualifying fill. No position or P&L exists; the next scan may publish a new experiment."
    elif state == "rejected":
        result["required_next_action"] = "The candidate failed admission. No position was opened; the next scan reevaluates new evidence."
    elif state == "unmeasurable":
        result["required_next_action"] = "The execution worker could not verify this observation. It is excluded from performance and learning until its evidence is reconciled."
    else:
        result["required_next_action"] = "The paper worker waits for a later executable quote inside the frozen entry limit and deadline."
    if state not in {"entered", "closed"}:
        # A failed observation is not a zero-P&L trade or a realized loss.
        result["net_pnl"] = result["net_return"] = None
    return result


def experiment_history(runtime: Any, *, observation_id: str, limit: int = 2000, now: datetime | None = None) -> dict[str, Any] | None:
    now = now or datetime.now(UTC)
    if _time(now) is None:
        raise ValueError("experiment history requires a timezone-aware cutoff")
    limit = max(1, min(int(limit), 2000))
    with runtime.snapshot(JOB_PROFILE) as connection:
        shadow = connection.execute(
            """SELECT shadow.id::text, instrument.symbol, shadow.status, shadow.entry_at,
                      shadow.entry_price, shadow.exit_at, shadow.exit_price,
                      shadow.metrics->'entry_quotes' AS entry_quotes,
                      shadow.metrics->>'observed_at' AS mark_at,
                      shadow.metrics->'current_return' AS net_return
               FROM analysis.shadow_trade shadow
               JOIN analysis.decision decision ON decision.id = shadow.decision_id
               JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
               WHERE shadow.id = %s::uuid AND shadow.source_kind = 'options_paper_experiment'""",
            [observation_id],
        ).fetchone()
        if shadow is None:
            return None
        rows = connection.execute(
            """SELECT event_key, kind, observed_at AS at, quote_observed_at, price,
                      net_pnl, net_return, fees, reason, evidence
               FROM analysis.option_experiment_event
               WHERE shadow_trade_id = %s::uuid AND observed_at <= %s
               ORDER BY observed_at DESC, CASE kind WHEN 'exit' THEN 2 WHEN 'entry' THEN 0 ELSE 1 END DESC, event_key
               LIMIT %s""", [observation_id, now, limit + 1],
        ).fetchall()
    truncated = len(rows) > limit
    events = [dict(row) for row in reversed(rows[:limit])]
    return {"observation_id": observation_id, "symbol": shadow["symbol"], "status": shadow["status"],
            "as_of": now, "events": events, "truncated": truncated,
            "entry_at": shadow["entry_at"], "entry_price": shadow["entry_price"],
            "exit_at": shadow["exit_at"], "exit_price": shadow["exit_price"],
            "history_started_at": events[0]["at"] if events else None,
            "coverage": "recorded_events" if events else "snapshot_only" if shadow["entry_at"] else "not_entered",
            "basis": "One-contract research experiment, not funded paper NAV. Only prospectively recorded marks are plotted; no earlier price path is inferred.",
            "quantity": 1, "multiplier": 100}


def experiment_progress(runtime: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Exact lifecycle counts and a bounded per-position liveness scan.

    An active position with a ticking worker but no usable quote is unhealthy.
    Exchange closures consume no management/freshness budget.
    """
    reference = now or datetime.now(UTC)
    with runtime.snapshot(JOB_PROFILE) as connection:
        rows = connection.execute(
            """SELECT status, count(*) AS count
               FROM analysis.shadow_trade
               WHERE source_kind = 'options_paper_experiment' AND created_at <= %s
               GROUP BY status""", [reference],
        ).fetchall()
        active = connection.execute(
            """SELECT shadow.id::text, instrument.symbol, shadow.status, shadow.created_at,
                      shadow.entry_at, shadow.metrics->>'last_checked_at' AS last_checked_at,
                      shadow.metrics->>'quote_observed_at' AS quote_observed_at,
                      shadow.metrics->'observed_quotes' AS observed_quotes,
                      shadow.metrics->>'entry_deadline' AS entry_deadline
               FROM analysis.shadow_trade shadow
               JOIN analysis.decision decision ON decision.id = shadow.decision_id
               JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
               WHERE shadow.source_kind = 'options_paper_experiment'
                 AND shadow.status IN ('pending', 'entered') AND shadow.created_at <= %s
               ORDER BY shadow.created_at, shadow.id LIMIT 2001""", [reference],
        ).fetchall()
    counts = {row["status"]: int(row["count"]) for row in rows}
    incidents: list[dict[str, Any]] = []
    checks, marks, deadlines = [], [], []
    for source in active[:2000]:
        row = dict(source)
        checked = _time(row.get("last_checked_at"))
        if checked:
            checks.append(checked)
        anchor = checked or row["entry_at"] or row["created_at"]
        if anchor > reference or market_session_elapsed(anchor, reference).total_seconds() > 300:
            incidents.append({"observation_id": row["id"], "symbol": row["symbol"],
                "reason": "experiment_management_overdue", "job": "process_options_paper_orders"})
        if row["status"] == "entered":
            valuation = observation_lifecycle(row, now=reference)
            if valuation.get("quote_observed_at"):
                marks.append(valuation["quote_observed_at"])
            if valuation["mark_status"] == "overdue":
                incidents.append({"observation_id": row["id"], "symbol": row["symbol"],
                    "reason": "experiment_quote_overdue", "job": "refresh_paper_quotes"})
        elif (deadline := _time(row.get("entry_deadline"))) is not None:
            deadlines.append(deadline)
    if len(active) > 2000:
        incidents.append({"reason": "experiment_liveness_scan_truncated", "job": "process_options_paper_orders"})
    return {"counts": counts, "active": counts.get("pending", 0) + counts.get("entered", 0),
            "completed": counts.get("closed", 0), "market_open": is_market_open(reference),
            "management_status": "overdue" if incidents else "market_closed" if active and not is_market_open(reference) else "collecting" if active else "idle",
            "incidents": incidents, "scanned": min(len(active), 2000), "truncated": len(active) > 2000,
            "last_checked_at": max(checks, default=None), "last_mark_at": max(marks, default=None),
            "next_entry_deadline": min(deadlines, default=None),
            "owner_job": "process_options_paper_orders", "as_of": reference,
            "basis": "Research collection only. Open observations and job runs are not completed independent evidence."}


def _time(value: Any) -> datetime | None:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None
