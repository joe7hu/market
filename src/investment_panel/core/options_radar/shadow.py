"""Shadow-trade lifecycle: open, mark, exit, and thesis-gated entry/exit."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.source_ingestion.utils import (stable_id)
from investment_panel.core.options_radar.coerce import (_elapsed_days, _integer, _iso, _json, _normalize_symbol, _number)
from investment_panel.core.options_radar.constants import (DEFAULT_STRATEGY_VERSION, EXPLORATION_MIN_POPULATION, EXPLORATION_SAMPLE_RATE)
from investment_panel.core.options_radar.indicators import (_bounded_abs_delta)
from investment_panel.core.options_radar.strategy_outcomes import (_first_hit_days, _realized_series, _return_at_horizon)


def _candidate_events_with_thesis(con: Any, strategy_version: str, *, state: str, min_snapshot_time: str | None = None) -> list[dict[str, Any]]:
    """Candidate events in ``state`` joined to their latest thesis validation (candidate-
    scoped, falling back to legacy ticker-scoped). Shared by the FIRE shadow-entry path
    and the SETUP exploration path so both apply the same thesis gating."""

    return query_rows(
        con,
        """
        WITH candidate_validation AS (
            SELECT *
            FROM agent_thesis_validation
            WHERE strategy_version = ? AND candidate_event_id IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY candidate_event_id ORDER BY validated_at DESC) = 1
        ),
        legacy_ticker_validation AS (
            SELECT *
            FROM agent_thesis_validation
            WHERE strategy_version = ? AND candidate_event_id IS NULL
            QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY validated_at DESC) = 1
        )
        SELECT
            ce.event_id,
            ce.snapshot_time,
            ce.premium_fill_assumption,
            ce.ticker,
            COALESCE(cv.state, lv.state) AS thesis_validation_state,
            COALESCE(cv.invalidation_status, lv.invalidation_status) AS thesis_invalidation_status,
            COALESCE(cv.red_team_status, lv.red_team_status) AS thesis_red_team_status
        FROM candidate_event ce
        LEFT JOIN candidate_validation cv
          ON cv.candidate_event_id = ce.event_id
         AND cv.strategy_version = ce.strategy_version
        LEFT JOIN legacy_ticker_validation lv
          ON lv.ticker = ce.ticker
         AND lv.strategy_version = ce.strategy_version
        WHERE ce.strategy_version = ? AND ce.state = ?
          AND (? IS NULL OR ce.snapshot_time >= TRY_CAST(? AS TIMESTAMP))
        ORDER BY ce.snapshot_time
        """,
        [strategy_version, strategy_version, strategy_version, state, min_snapshot_time, min_snapshot_time],
    )


def _exploration_sampled(event_id: Any, sample_rate: float) -> bool:
    """Deterministic per-event epsilon sample in [0, sample_rate): stable across runs so a
    candidate is either always or never an exploration pick (no run-to-run churn)."""

    digest = hashlib.sha1(str(event_id).encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % 10_000) / 10_000.0 < sample_rate


def _insert_shadow_trade(con: Any, row: dict[str, Any], *, authority: str, created_from: str) -> int:
    trade_id = stable_id("shadow_trade", row["event_id"])
    before = query_rows(con, "SELECT count(*) AS count FROM shadow_trade WHERE trade_id = ?", [trade_id])[0]["count"]
    con.execute(
        """
        INSERT OR IGNORE INTO shadow_trade
        (trade_id, event_id, entry_time, entry_price_assumption, exit_time, exit_price,
         status, max_return_seen, max_drawdown_seen, time_to_2x, time_to_5x, time_to_10x,
         exit_reason, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            trade_id,
            row["event_id"],
            row["snapshot_time"],
            _number(row["premium_fill_assumption"]),
            None,
            None,
            "open",
            0.0,
            0.0,
            None,
            None,
            None,
            None,
            json_dumps({"authority": authority, "created_from": created_from}),
        ],
    )
    after = query_rows(con, "SELECT count(*) AS count FROM shadow_trade WHERE trade_id = ?", [trade_id])[0]["count"]
    return int(after) - int(before)


def create_shadow_trades(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION, min_snapshot_time: str | None = None) -> int:
    rows = _candidate_events_with_thesis(con, strategy_version, state="FIRE", min_snapshot_time=min_snapshot_time)
    count = 0
    for row in rows:
        if _thesis_validation_blocks_entry(row):
            continue
        count += _insert_shadow_trade(con, row, authority="shadow_only", created_from="candidate_event")
    return count


def create_exploration_shadow_trades(
    con: Any,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    sample_rate: float = EXPLORATION_SAMPLE_RATE,
    min_population: int = EXPLORATION_MIN_POPULATION,
    min_snapshot_time: str | None = None,
) -> int:
    """Epsilon-exploration: shadow-trade a deterministic sample of SETUP (near-miss)
    candidates the gates did *not* fire, so the learning loop observes realized outcomes
    from the rejected region instead of only ever seeing contracts that passed every gate
    (the classic 'you only learn about what you selected' bias). Tagged
    ``shadow_exploration`` and only run once the SETUP population clears a floor, so it
    never fabricates trades from a thin tape — and FIRE-only cohort/backtest metrics stay
    unpolluted."""

    rows = _candidate_events_with_thesis(con, strategy_version, state="SETUP", min_snapshot_time=min_snapshot_time)
    if len(rows) < min_population:
        return 0
    count = 0
    for row in rows:
        if not _exploration_sampled(row["event_id"], sample_rate):
            continue
        if _thesis_validation_blocks_entry(row):
            continue
        count += _insert_shadow_trade(con, row, authority="shadow_exploration", created_from="setup_candidate_event")
    return count


def apply_shadow_trade_exits(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    rows = query_rows(
        con,
        """
        WITH latest_exit AS (
            SELECT
                transition_id,
                snapshot_time,
                state,
                trigger_reason,
                trade_id,
                mark_id,
                row_number() OVER (
                    PARTITION BY trade_id
                    ORDER BY snapshot_time DESC, evaluated_at DESC
                ) AS rn
            FROM radar_state_transition
            WHERE strategy_version = ?
                  AND trade_id IS NOT NULL
                  AND state IN ('EXIT', 'INVALIDATED')
        )
        SELECT
            st.trade_id,
            st.entry_price_assumption,
            st.raw,
            latest_exit.transition_id,
            latest_exit.snapshot_time,
            latest_exit.state,
            latest_exit.trigger_reason,
            stm.mark_price
        FROM latest_exit
        JOIN shadow_trade st ON st.trade_id = latest_exit.trade_id
        LEFT JOIN shadow_trade_mark stm ON stm.mark_id = latest_exit.mark_id
        WHERE latest_exit.rn = 1
              AND COALESCE(st.status, 'open') = 'open'
        """,
        [strategy_version],
    )
    count = 0
    for row in rows:
        exit_time = _iso(row.get("snapshot_time"))
        exit_price = _number(row.get("mark_price"))
        if exit_price is None:
            exit_price = _number(row.get("entry_price_assumption"))
        raw = {
            **_json(row.get("raw")),
            "exit_state": row.get("state"),
            "exit_transition_id": row.get("transition_id"),
            "exit_authority": "deterministic_radar_state",
        }
        con.execute(
            """
            UPDATE shadow_trade
            SET exit_time = ?,
                exit_price = ?,
                status = 'closed',
                exit_reason = ?,
                raw = ?
            WHERE trade_id = ?
            """,
            [exit_time, exit_price, row.get("trigger_reason"), json_dumps(raw), row.get("trade_id")],
        )
        count += 1
    return count


def mark_shadow_trades(con: Any) -> int:
    trades = query_rows(
        con,
        """
        SELECT
            st.*,
            ce.contract_id
        FROM candidate_event ce
        JOIN shadow_trade st ON st.event_id = ce.event_id
        WHERE st.status = 'open'
        """,
    )
    count = 0
    for trade in trades:
        latest = query_rows(
            con,
            """
            SELECT snapshot_time, mid
            FROM option_snapshot
            WHERE contract_id = ? AND snapshot_time >= TRY_CAST(? AS TIMESTAMP)
            ORDER BY snapshot_time DESC
            LIMIT 1
            """,
            [trade["contract_id"], trade["entry_time"]],
        )
        if not latest:
            continue
        current_mid = _number(latest[0].get("mid"))
        entry_price = _number(trade.get("entry_price_assumption"))
        if current_mid is None or entry_price is None or entry_price <= 0:
            continue
        current_return = current_mid / entry_price - 1
        max_return = max(_number(trade.get("max_return_seen")) or 0.0, current_return)
        max_drawdown = min(_number(trade.get("max_drawdown_seen")) or 0.0, current_return)
        time_to_2x = trade.get("time_to_2x") or (_elapsed_days(trade.get("entry_time"), latest[0].get("snapshot_time")) if current_return >= 1.0 else None)
        time_to_5x = trade.get("time_to_5x") or (_elapsed_days(trade.get("entry_time"), latest[0].get("snapshot_time")) if current_return >= 4.0 else None)
        time_to_10x = trade.get("time_to_10x") or (_elapsed_days(trade.get("entry_time"), latest[0].get("snapshot_time")) if current_return >= 9.0 else None)
        con.execute(
            """
            UPDATE shadow_trade
            SET max_return_seen = ?, max_drawdown_seen = ?, time_to_2x = ?,
                time_to_5x = ?, time_to_10x = ?, raw = ?
            WHERE trade_id = ?
            """,
            [
                max_return,
                max_drawdown,
                time_to_2x,
                time_to_5x,
                time_to_10x,
                json_dumps({"last_mark": latest[0]["snapshot_time"], "current_mid": current_mid, "current_return": current_return}),
                trade["trade_id"],
            ],
        )
        count += 1
    return count


def refresh_shadow_trade_marks(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION, min_entry_time: str | None = None) -> int:
    trades = query_rows(
        con,
        """
        WITH snapshot_max AS (
            SELECT contract_id, max(snapshot_time) AS max_snapshot_time
            FROM option_snapshot
            GROUP BY contract_id
        ),
        mark_max AS (
            SELECT trade_id, max(mark_time) AS max_mark_time
            FROM shadow_trade_mark
            WHERE strategy_version = ?
            GROUP BY trade_id
        )
        SELECT
            st.*,
            ce.contract_id,
            ce.ticker,
            ce.strategy_version
        FROM shadow_trade st
        JOIN candidate_event ce ON ce.event_id = st.event_id
        JOIN snapshot_max sm ON sm.contract_id = ce.contract_id
        LEFT JOIN mark_max mm ON mm.trade_id = st.trade_id
        WHERE ce.strategy_version = ?
          AND (? IS NULL OR st.entry_time >= TRY_CAST(? AS TIMESTAMP))
          AND (mm.max_mark_time IS NULL OR sm.max_snapshot_time > mm.max_mark_time)
        """,
        [strategy_version, strategy_version, min_entry_time, min_entry_time],
    )
    count = 0
    for trade in trades:
        snapshots = query_rows(
            con,
            """
            SELECT *
            FROM option_snapshot
            WHERE contract_id = ? AND snapshot_time >= TRY_CAST(? AS TIMESTAMP)
            ORDER BY snapshot_time
            """,
            [trade["contract_id"], trade["entry_time"]],
        )
        for mark in build_shadow_trade_marks(trade, snapshots):
            con.execute(
                """
                INSERT OR REPLACE INTO shadow_trade_mark
                (mark_id, trade_id, event_id, contract_id, ticker, strategy_version,
                 mark_time, entry_time, entry_price_assumption, mark_price,
                 current_return, return_1d, return_5d, return_20d, return_60d,
                 max_return_since_alert, max_drawdown_since_alert, time_to_2x,
                 time_to_5x, time_to_10x, dte, spread_pct, iv, underlying_price,
                 expired_worthless_probability_change, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    mark["mark_id"],
                    mark["trade_id"],
                    mark["event_id"],
                    mark["contract_id"],
                    mark["ticker"],
                    mark["strategy_version"],
                    mark["mark_time"],
                    mark["entry_time"],
                    mark["entry_price_assumption"],
                    mark["mark_price"],
                    mark["current_return"],
                    mark["return_1d"],
                    mark["return_5d"],
                    mark["return_20d"],
                    mark["return_60d"],
                    mark["max_return_since_alert"],
                    mark["max_drawdown_since_alert"],
                    mark["time_to_2x"],
                    mark["time_to_5x"],
                    mark["time_to_10x"],
                    mark["dte"],
                    mark["spread_pct"],
                    mark["iv"],
                    mark["underlying_price"],
                    mark["expired_worthless_probability_change"],
                    json_dumps(mark["raw"]),
                ],
            )
            count += 1
    return count


def build_shadow_trade_marks(trade: dict[str, Any], snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entry_price = _number(trade.get("entry_price_assumption"))
    if entry_price is None or entry_price <= 0:
        return []
    clean_snapshots = [row for row in snapshots if _number(row.get("mid")) is not None]
    if not clean_snapshots:
        return []
    entry_time = _iso(trade.get("entry_time"))
    entry_delta = _bounded_abs_delta(clean_snapshots[0].get("delta"))
    full_returns = [(_iso(row.get("snapshot_time")), _number(row.get("mid")) / entry_price - 1) for row in clean_snapshots]
    realized_path = _realized_series(full_returns)
    returns: list[tuple[Any, float]] = []
    marks: list[dict[str, Any]] = []
    for index, snapshot in enumerate(clean_snapshots):
        mark_price = _number(snapshot.get("mid"))
        if mark_price is None:
            continue
        mark_time = _iso(snapshot.get("snapshot_time"))
        current_return = mark_price / entry_price - 1
        returns.append((mark_time, current_return))
        values = [value for _time, value in returns]
        mark_delta = _bounded_abs_delta(snapshot.get("delta"))
        realized_return = realized_path[index]
        worthless_change = None if entry_delta is None or mark_delta is None else entry_delta - mark_delta
        marks.append(
            {
                "mark_id": stable_id("shadow_trade_mark", trade.get("trade_id"), mark_time),
                "trade_id": trade.get("trade_id"),
                "event_id": trade.get("event_id"),
                "contract_id": trade.get("contract_id"),
                "ticker": _normalize_symbol(trade.get("ticker") or snapshot.get("ticker")),
                "strategy_version": trade.get("strategy_version"),
                "mark_time": mark_time,
                "entry_time": entry_time,
                "entry_price_assumption": entry_price,
                "mark_price": mark_price,
                "current_return": current_return,
                "return_1d": _return_at_horizon(entry_time, returns, 1, mark_time),
                "return_5d": _return_at_horizon(entry_time, returns, 5, mark_time),
                "return_20d": _return_at_horizon(entry_time, returns, 20, mark_time),
                "return_60d": _return_at_horizon(entry_time, returns, 60, mark_time),
                "max_return_since_alert": max(values),
                "max_drawdown_since_alert": min(values),
                "time_to_2x": _first_hit_days(entry_time, returns, 1.0),
                "time_to_5x": _first_hit_days(entry_time, returns, 4.0),
                "time_to_10x": _first_hit_days(entry_time, returns, 9.0),
                "dte": _integer(snapshot.get("dte")),
                "spread_pct": _number(snapshot.get("spread_pct")),
                "iv": _number(snapshot.get("iv")),
                "underlying_price": _number(snapshot.get("underlying_price")),
                "expired_worthless_probability_change": worthless_change,
                "raw": {
                    "authority": "shadow_validation_only",
                    "return_horizon_method": "first_snapshot_at_or_after_horizon",
                    "realized_exit_return": realized_return,
                    "realized_exit_basis": "trailing_stop_capturable",
                    "expired_worthless_probability_proxy": "abs(entry_delta)-abs(mark_delta)",
                    "entry_delta_abs": entry_delta,
                    "mark_delta_abs": mark_delta,
                },
            }
        )
    return marks


def _shadow_trades_by_contract(con: Any, strategy_version: str) -> dict[str, list[dict[str, Any]]]:
    rows = query_rows(
        con,
        """
        SELECT
            st.*,
            ce.contract_id,
            ce.ticker,
            ce.strategy_version
        FROM shadow_trade st
        JOIN candidate_event ce ON ce.event_id = st.event_id
        WHERE ce.strategy_version = ?
        ORDER BY ce.contract_id, st.entry_time
        """,
        [strategy_version],
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("contract_id") or "")].append(row)
    return grouped


def _shadow_marks_by_trade(con: Any, strategy_version: str) -> dict[str, list[dict[str, Any]]]:
    rows = query_rows(
        con,
        """
        SELECT *
        FROM shadow_trade_mark
        WHERE strategy_version = ?
        ORDER BY trade_id, mark_time
        """,
        [strategy_version],
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("trade_id") or "")].append(row)
    return grouped


def _latest_thesis_validation_by_candidate_event(con: Any, strategy_version: str) -> dict[str, dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT *
        FROM agent_thesis_validation
        WHERE strategy_version = ? AND candidate_event_id IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY candidate_event_id ORDER BY validated_at DESC) = 1
        """,
        [strategy_version],
    )
    return {str(row.get("candidate_event_id")): row for row in rows if row.get("candidate_event_id")}


def _latest_legacy_thesis_validation_by_ticker(con: Any, strategy_version: str) -> dict[str, dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT *
        FROM agent_thesis_validation
        WHERE strategy_version = ? AND candidate_event_id IS NULL
        QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY validated_at DESC) = 1
        """,
        [strategy_version],
    )
    return {_normalize_symbol(row.get("ticker")): row for row in rows if row.get("ticker")}


def _trade_for_snapshot(trades: list[dict[str, Any]], snapshot_time: str) -> dict[str, Any] | None:
    active = [trade for trade in trades if _iso(trade.get("entry_time")) <= snapshot_time]
    if not active:
        return None
    return active[-1]


def _mark_for_snapshot(marks: list[dict[str, Any]], snapshot_time: str) -> dict[str, Any] | None:
    active = [mark for mark in marks if _iso(mark.get("mark_time")) <= snapshot_time]
    if not active:
        return None
    return active[-1]


def _thesis_validation_blocks_entry(row: dict[str, Any]) -> bool:
    return _thesis_exit_reason(
        {
            "state": row.get("thesis_validation_state"),
            "invalidation_status": row.get("thesis_invalidation_status"),
            "red_team_status": row.get("thesis_red_team_status"),
        }
    ) is not None


def _thesis_exit_reason(thesis_validation: dict[str, Any] | None) -> str | None:
    if not thesis_validation:
        return None
    validation_state = str(thesis_validation.get("state") or "").lower()
    invalidation_status = str(thesis_validation.get("invalidation_status") or "").lower()
    red_team_status = str(thesis_validation.get("red_team_status") or "").lower()
    if validation_state == "invalidated" or invalidation_status == "breached":
        return "agent_thesis_invalidated"
    if red_team_status == "hard_risk_triggered":
        return "hard_red_team_risk"
    return None
