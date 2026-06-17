"""Candidate-event marks and attributions over the event-sourced history."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.source_ingestion.utils import (stable_id)
from investment_panel.core.options_radar.coerce import (_elapsed_days, _integer, _iso, _normalize_symbol, _number)
from investment_panel.core.options_radar.constants import (DEFAULT_STRATEGY_VERSION)
from investment_panel.core.options_radar.indicators import (_bounded_abs_delta, _diff)
from investment_panel.core.options_radar.strategy_common import (_attribution_label)
from investment_panel.core.options_radar.strategy_outcomes import (_first_hit_days, _realized_series, _return_at_horizon)

def refresh_candidate_event_marks(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    events = query_rows(
        con,
        """
        SELECT *
        FROM candidate_event
        WHERE strategy_version = ?
        ORDER BY snapshot_time, ticker, contract_id
        """,
        [strategy_version],
    )
    con.execute("DELETE FROM candidate_event_mark WHERE strategy_version = ?", [strategy_version])
    count = 0
    for event in events:
        snapshots = query_rows(
            con,
            """
            SELECT *
            FROM option_snapshot
            WHERE contract_id = ? AND snapshot_time >= TRY_CAST(? AS TIMESTAMP)
            ORDER BY snapshot_time
            """,
            [event["contract_id"], event["snapshot_time"]],
        )
        for mark in build_candidate_event_marks(event, snapshots):
            con.execute(
                """
                INSERT OR REPLACE INTO candidate_event_mark
                (mark_id, event_id, contract_id, ticker, strategy_version,
                 candidate_state, mark_time, alert_time, premium_fill_assumption,
                 mark_price, current_return, return_1d, return_5d, return_20d,
                 return_60d, max_return_since_alert, max_drawdown_since_alert,
                 time_to_2x, time_to_5x, time_to_10x, dte, spread_pct, iv,
                 underlying_price, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    mark["mark_id"],
                    mark["event_id"],
                    mark["contract_id"],
                    mark["ticker"],
                    mark["strategy_version"],
                    mark["candidate_state"],
                    mark["mark_time"],
                    mark["alert_time"],
                    mark["premium_fill_assumption"],
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
                    json_dumps(mark["raw"]),
                ],
            )
            count += 1
    return count


def build_candidate_event_marks(event: dict[str, Any], snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entry_price = _number(event.get("premium_fill_assumption"))
    if entry_price is None or entry_price <= 0:
        return []
    clean_snapshots = [row for row in snapshots if _number(row.get("mid")) is not None]
    if not clean_snapshots:
        return []
    alert_time = _iso(event.get("snapshot_time"))
    entry_delta = _bounded_abs_delta(clean_snapshots[0].get("delta"))
    # Point-in-time realizable return per mark (trailing-stop exit); the latest mark
    # carries the event's capturable outcome that calibration reads.
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
        marks.append(
            {
                "mark_id": stable_id("candidate_event_mark", event.get("event_id"), mark_time),
                "event_id": event.get("event_id"),
                "contract_id": event.get("contract_id"),
                "ticker": _normalize_symbol(event.get("ticker") or snapshot.get("ticker")),
                "strategy_version": event.get("strategy_version"),
                "candidate_state": str(event.get("state") or "").upper(),
                "mark_time": mark_time,
                "alert_time": alert_time,
                "premium_fill_assumption": entry_price,
                "mark_price": mark_price,
                "current_return": current_return,
                "return_1d": _return_at_horizon(alert_time, returns, 1, mark_time),
                "return_5d": _return_at_horizon(alert_time, returns, 5, mark_time),
                "return_20d": _return_at_horizon(alert_time, returns, 20, mark_time),
                "return_60d": _return_at_horizon(alert_time, returns, 60, mark_time),
                "max_return_since_alert": max(values),
                "max_drawdown_since_alert": min(values),
                "time_to_2x": _first_hit_days(alert_time, returns, 1.0),
                "time_to_5x": _first_hit_days(alert_time, returns, 4.0),
                "time_to_10x": _first_hit_days(alert_time, returns, 9.0),
                "dte": _integer(snapshot.get("dte")),
                "spread_pct": _number(snapshot.get("spread_pct")),
                "iv": _number(snapshot.get("iv")),
                "underlying_price": _number(snapshot.get("underlying_price")),
                "raw": {
                    "authority": "candidate_validation_only",
                    "candidate_state": str(event.get("state") or "").upper(),
                    "trigger_reason": event.get("trigger_reason"),
                    "return_horizon_method": "first_snapshot_at_or_after_horizon",
                    "realized_exit_return": realized_return,
                    "realized_exit_basis": "trailing_stop_capturable",
                    "entry_delta_abs": entry_delta,
                    "mark_delta_abs": mark_delta,
                },
            }
        )
    return marks


def refresh_candidate_event_attributions(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    rows = query_rows(
        con,
        """
        SELECT
            m.*,
            s.mid AS snapshot_mid,
            s.underlying_price AS snapshot_underlying_price,
            s.iv AS snapshot_iv,
            s.spread_pct AS snapshot_spread_pct,
            s.delta,
            s.vega,
            s.theta
        FROM candidate_event_mark m
        LEFT JOIN option_snapshot s
          ON s.contract_id = m.contract_id
         AND s.snapshot_time = m.mark_time
        WHERE m.strategy_version = ?
        ORDER BY m.event_id, m.mark_time
        """,
        [strategy_version],
    )
    con.execute("DELETE FROM candidate_event_attribution WHERE strategy_version = ?", [strategy_version])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        event_id = str(row.get("event_id") or "")
        if event_id:
            grouped[event_id].append(row)

    count = 0
    for marks in grouped.values():
        for index in range(1, len(marks)):
            attribution = build_candidate_event_attribution(marks[index - 1], marks[index])
            if not attribution:
                continue
            con.execute(
                """
                INSERT OR REPLACE INTO candidate_event_attribution
                (attribution_id, event_id, contract_id, ticker, strategy_version,
                 candidate_state, snapshot_time, prior_snapshot_time,
                 option_return, underlying_return, iv_change, theta_decay,
                 spread_change, stock_move_effect, iv_effect, theta_effect,
                 spread_effect, unexplained_effect, label, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    attribution["attribution_id"],
                    attribution["event_id"],
                    attribution["contract_id"],
                    attribution["ticker"],
                    attribution["strategy_version"],
                    attribution["candidate_state"],
                    attribution["snapshot_time"],
                    attribution["prior_snapshot_time"],
                    attribution["option_return"],
                    attribution["underlying_return"],
                    attribution["iv_change"],
                    attribution["theta_decay"],
                    attribution["spread_change"],
                    attribution["stock_move_effect"],
                    attribution["iv_effect"],
                    attribution["theta_effect"],
                    attribution["spread_effect"],
                    attribution["unexplained_effect"],
                    attribution["label"],
                    json_dumps(attribution["raw"]),
                ],
            )
            count += 1
    return count


def build_candidate_event_attribution(prior: dict[str, Any], latest: dict[str, Any]) -> dict[str, Any] | None:
    prior_mid = _number(prior.get("mark_price"))
    if prior_mid is None:
        prior_mid = _number(prior.get("snapshot_mid"))
    latest_mid = _number(latest.get("mark_price"))
    if latest_mid is None:
        latest_mid = _number(latest.get("snapshot_mid"))
    if prior_mid is None or latest_mid is None or prior_mid <= 0:
        return None

    prior_underlying = _number(prior.get("underlying_price"))
    if prior_underlying is None:
        prior_underlying = _number(prior.get("snapshot_underlying_price"))
    latest_underlying = _number(latest.get("underlying_price"))
    if latest_underlying is None:
        latest_underlying = _number(latest.get("snapshot_underlying_price"))
    underlying_change = None if prior_underlying is None or latest_underlying is None else latest_underlying - prior_underlying
    underlying_return = None if prior_underlying is None or prior_underlying <= 0 or latest_underlying is None else latest_underlying / prior_underlying - 1

    prior_iv = _number(prior.get("iv"))
    if prior_iv is None:
        prior_iv = _number(prior.get("snapshot_iv"))
    latest_iv = _number(latest.get("iv"))
    if latest_iv is None:
        latest_iv = _number(latest.get("snapshot_iv"))
    iv_change = _diff(latest_iv, prior_iv)

    prior_spread = _number(prior.get("spread_pct"))
    if prior_spread is None:
        prior_spread = _number(prior.get("snapshot_spread_pct"))
    latest_spread = _number(latest.get("spread_pct"))
    if latest_spread is None:
        latest_spread = _number(latest.get("snapshot_spread_pct"))
    spread_change = _diff(latest_spread, prior_spread)

    days = max(1, _elapsed_days(prior.get("mark_time"), latest.get("mark_time")) or 1)
    theta_decay = (_number(prior.get("theta")) or 0.0) * days
    stock_move_effect = ((_number(prior.get("delta")) or 0.0) * (underlying_change or 0.0)) / prior_mid
    iv_effect = ((_number(prior.get("vega")) or 0.0) * (iv_change or 0.0)) / prior_mid
    theta_effect = theta_decay / prior_mid
    spread_effect = -(spread_change or 0.0)
    option_return = latest_mid / prior_mid - 1
    explained = stock_move_effect + iv_effect + theta_effect + spread_effect
    unexplained = option_return - explained
    label = _attribution_label(option_return, underlying_return, iv_change, theta_effect, spread_change)
    return {
        "attribution_id": stable_id("candidate_event_attribution", latest.get("event_id"), latest.get("mark_time")),
        "event_id": latest.get("event_id"),
        "contract_id": latest.get("contract_id"),
        "ticker": _normalize_symbol(latest.get("ticker")),
        "strategy_version": latest.get("strategy_version"),
        "candidate_state": str(latest.get("candidate_state") or "").upper(),
        "snapshot_time": _iso(latest.get("mark_time")),
        "prior_snapshot_time": _iso(prior.get("mark_time")),
        "option_return": option_return,
        "underlying_return": underlying_return,
        "iv_change": iv_change,
        "theta_decay": theta_decay,
        "spread_change": spread_change,
        "stock_move_effect": stock_move_effect,
        "iv_effect": iv_effect,
        "theta_effect": theta_effect,
        "spread_effect": spread_effect,
        "unexplained_effect": unexplained,
        "label": label,
        "raw": {
            "authority": "candidate_attribution_only",
            "days": days,
            "prior_mark_id": prior.get("mark_id"),
            "latest_mark_id": latest.get("mark_id"),
            "prior_mid": prior_mid,
            "latest_mid": latest_mid,
            "prior_underlying": prior_underlying,
            "latest_underlying": latest_underlying,
            "method": "candidate_mark_delta_vega_theta_spread_approximation",
        },
    }
