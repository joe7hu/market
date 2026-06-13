"""Option attributions and missed-winner detection."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.source_ingestion.utils import (stable_id)
from investment_panel.core.options_radar.coerce import (_elapsed_days, _iso, _normalize_symbol, _number)
from investment_panel.core.options_radar.constants import (DEFAULT_STRATEGY_VERSION)
from investment_panel.core.options_radar.dbutil import (_compact_snapshot, _source_filter, _symbol_filter)
from investment_panel.core.options_radar.indicators import (_diff)
from investment_panel.core.options_radar.strategy_common import (_attribution_label, _missed_filter_reason, _proposed_family)

def refresh_option_attributions(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    trades = query_rows(
        con,
        """
        SELECT st.trade_id, st.event_id, st.entry_time, ce.contract_id, ce.strategy_version
        FROM shadow_trade st
        JOIN candidate_event ce ON ce.event_id = st.event_id
        WHERE ce.strategy_version = ?
        """,
        [strategy_version],
    )
    count = 0
    for trade in trades:
        latest_rows = query_rows(
            con,
            """
            SELECT *
            FROM option_snapshot
            WHERE contract_id = ? AND snapshot_time >= TRY_CAST(? AS TIMESTAMP)
            ORDER BY snapshot_time DESC
            LIMIT 1
            """,
            [trade["contract_id"], trade["entry_time"]],
        )
        if not latest_rows:
            continue
        latest = latest_rows[0]
        prior_rows = query_rows(
            con,
            """
            SELECT *
            FROM option_snapshot
            WHERE contract_id = ? AND snapshot_time < TRY_CAST(? AS TIMESTAMP)
                  AND snapshot_time >= TRY_CAST(? AS TIMESTAMP)
            ORDER BY snapshot_time DESC
            LIMIT 1
            """,
            [trade["contract_id"], latest["snapshot_time"], trade["entry_time"]],
        )
        if not prior_rows:
            prior_rows = query_rows(
                con,
                """
                SELECT *
                FROM option_snapshot
                WHERE contract_id = ? AND snapshot_time = TRY_CAST(? AS TIMESTAMP)
                LIMIT 1
                """,
                [trade["contract_id"], trade["entry_time"]],
            )
        if not prior_rows or prior_rows[0]["snapshot_time"] == latest["snapshot_time"]:
            continue
        attribution = build_option_attribution(trade, prior_rows[0], latest)
        if not attribution:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO option_attribution
            (attribution_id, trade_id, event_id, contract_id, snapshot_time,
             prior_snapshot_time, option_return, underlying_return, iv_change,
             theta_decay, spread_change, stock_move_effect, iv_effect,
             theta_effect, spread_effect, unexplained_effect, label, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                attribution["attribution_id"],
                attribution["trade_id"],
                attribution["event_id"],
                attribution["contract_id"],
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


def build_option_attribution(trade: dict[str, Any], prior: dict[str, Any], latest: dict[str, Any]) -> dict[str, Any] | None:
    prior_mid = _number(prior.get("mid"))
    latest_mid = _number(latest.get("mid"))
    if prior_mid is None or latest_mid is None or prior_mid <= 0:
        return None
    prior_underlying = _number(prior.get("underlying_price"))
    latest_underlying = _number(latest.get("underlying_price"))
    underlying_change = None if prior_underlying is None or latest_underlying is None else latest_underlying - prior_underlying
    underlying_return = None if prior_underlying is None or prior_underlying <= 0 or latest_underlying is None else latest_underlying / prior_underlying - 1
    iv_change = _diff(_number(latest.get("iv")), _number(prior.get("iv")))
    days = max(1, _elapsed_days(prior.get("snapshot_time"), latest.get("snapshot_time")) or 1)
    theta_decay = (_number(prior.get("theta")) or 0.0) * days
    stock_move_effect = ((_number(prior.get("delta")) or 0.0) * (underlying_change or 0.0)) / prior_mid
    iv_effect = ((_number(prior.get("vega")) or 0.0) * (iv_change or 0.0)) / prior_mid
    theta_effect = theta_decay / prior_mid
    spread_change = _diff(_number(latest.get("spread_pct")), _number(prior.get("spread_pct")))
    spread_effect = -(spread_change or 0.0)
    option_return = latest_mid / prior_mid - 1
    explained = stock_move_effect + iv_effect + theta_effect + spread_effect
    unexplained = option_return - explained
    label = _attribution_label(option_return, underlying_return, iv_change, theta_effect, spread_change)
    return {
        "attribution_id": stable_id("option_attribution", trade["trade_id"], latest["snapshot_time"]),
        "trade_id": trade["trade_id"],
        "event_id": trade["event_id"],
        "contract_id": trade["contract_id"],
        "snapshot_time": _iso(latest.get("snapshot_time")),
        "prior_snapshot_time": _iso(prior.get("snapshot_time")),
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
            "days": days,
            "prior_mid": prior_mid,
            "latest_mid": latest_mid,
            "prior_underlying": prior_underlying,
            "latest_underlying": latest_underlying,
            "method": "delta_vega_theta_spread_approximation",
        },
    }


def detect_missed_winners(
    con: Any,
    symbols: list[str] | None = None,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    source: str | None = None,
) -> int:
    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    source_filter = _source_filter(source, table_alias="s", column="data_source")
    rows = query_rows(
        con,
        f"""
        SELECT *
        FROM option_snapshot s
        WHERE 1 = 1 {source_filter["sql"]} {symbol_filter["sql"]}
        ORDER BY s.contract_id, s.snapshot_time
        """,
        [*source_filter["params"], *symbol_filter["params"]],
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("contract_id")), []).append(row)

    count = 0
    for contract_id, snapshots in grouped.items():
        winner = build_missed_winner(con, contract_id, snapshots, strategy_version)
        if not winner:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO missed_winner_event
            (missed_id, detected_at, ticker, contract_id, strategy_version,
             first_snapshot_time, winner_snapshot_time, entry_price_assumption,
             winner_price, max_return_seen, winner_threshold, filter_reason,
             proposed_strategy_family, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                winner["missed_id"],
                winner["detected_at"],
                winner["ticker"],
                winner["contract_id"],
                winner["strategy_version"],
                winner["first_snapshot_time"],
                winner["winner_snapshot_time"],
                winner["entry_price_assumption"],
                winner["winner_price"],
                winner["max_return_seen"],
                winner["winner_threshold"],
                winner["filter_reason"],
                winner["proposed_strategy_family"],
                json_dumps(winner["raw"]),
            ],
        )
        count += 1
    return count


def build_missed_winner(con: Any, contract_id: str, snapshots: list[dict[str, Any]], strategy_version: str) -> dict[str, Any] | None:
    usable = [row for row in snapshots if (_number(row.get("mid")) or 0) > 0]
    if len(usable) < 2:
        return None
    entry = usable[0]
    entry_mid = _number(entry.get("mid"))
    if entry_mid is None or entry_mid <= 0:
        return None
    winner = max(usable[1:], key=lambda row: _number(row.get("mid")) or 0)
    winner_mid = _number(winner.get("mid"))
    if winner_mid is None:
        return None
    max_return = winner_mid / entry_mid - 1
    if max_return < 4.0:
        return None
    fire_rows = query_rows(
        con,
        """
        SELECT event_id
        FROM candidate_event
        WHERE contract_id = ? AND strategy_version = ? AND state = 'FIRE'
              AND snapshot_time <= TRY_CAST(? AS TIMESTAMP)
        LIMIT 1
        """,
        [contract_id, strategy_version, winner["snapshot_time"]],
    )
    if fire_rows:
        return None
    candidate_rows = query_rows(
        con,
        """
        SELECT state, trigger_reason, raw
        FROM candidate_event
        WHERE contract_id = ? AND strategy_version = ?
        ORDER BY snapshot_time
        LIMIT 1
        """,
        [contract_id, strategy_version],
    )
    filter_reason = _missed_filter_reason(candidate_rows[0] if candidate_rows else None)
    threshold = "10x" if max_return >= 9.0 else "5x"
    proposed_family = _proposed_family(filter_reason)
    return {
        "missed_id": stable_id("missed_winner", strategy_version, contract_id, entry["snapshot_time"], winner["snapshot_time"], threshold),
        "detected_at": datetime.utcnow().isoformat(),
        "ticker": _normalize_symbol(entry.get("ticker")),
        "contract_id": contract_id,
        "strategy_version": strategy_version,
        "first_snapshot_time": _iso(entry.get("snapshot_time")),
        "winner_snapshot_time": _iso(winner.get("snapshot_time")),
        "entry_price_assumption": entry_mid,
        "winner_price": winner_mid,
        "max_return_seen": max_return,
        "winner_threshold": threshold,
        "filter_reason": filter_reason,
        "proposed_strategy_family": proposed_family,
        "raw": {
            "candidate_state": candidate_rows[0].get("state") if candidate_rows else "missing_candidate",
            "first_snapshot": _compact_snapshot(entry),
            "winner_snapshot": _compact_snapshot(winner),
        },
    }
