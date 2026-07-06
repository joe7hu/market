"""Option attributions and missed-winner detection."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.source_ingestion.utils import (stable_id)
from investment_panel.core.options_radar.coerce import (_elapsed_days, _elapsed_hours, _iso, _json, _normalize_symbol, _number)
from investment_panel.core.options_radar.constants import (DEFAULT_STRATEGY_VERSION)
from investment_panel.core.options_radar.dbutil import (_compact_snapshot, _source_filter, _symbol_filter)
from investment_panel.core.options_radar.indicators import (_diff)
from investment_panel.core.options_radar.strategy_common import (_attribution_label, _missed_filter_reason, _proposed_family)
from investment_panel.core.options_radar.strategy_outcomes import (realized_exit_return)

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
    # A genuine missed winner has to have been *capturable*, not just a one-mark paper
    # spike. Score the contract on its realizable trailing-stop exit so we only propose
    # loosening a gate for a winner a trader could actually have realized.
    returns = [(_iso(row.get("snapshot_time")), (_number(row.get("mid")) or 0) / entry_mid - 1) for row in usable]
    realized = realized_exit_return(returns)
    if realized is None:
        return None
    realized_time, max_return = realized
    if max_return < 4.0:
        return None
    winner = next((row for row in usable if _iso(row.get("snapshot_time")) == realized_time), usable[-1])
    winner_mid = _number(winner.get("mid"))
    if winner_mid is None:
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
        SELECT event_id, snapshot_time, state, score, trigger_reason, raw
        FROM candidate_event
        WHERE contract_id = ? AND strategy_version = ?
        ORDER BY snapshot_time
        """,
        [contract_id, strategy_version],
    )
    first_candidate = candidate_rows[0] if candidate_rows else None
    filter_reason = _missed_filter_reason(first_candidate)
    threshold = "10x" if max_return >= 9.0 else "5x"
    option_type = str(entry.get("option_type") or "").strip().lower() or None
    proposed_family = _proposed_family(filter_reason, option_type=option_type)
    winner_time = _iso(winner.get("snapshot_time"))
    observed_peak_time, observed_peak_return = max(returns, key=lambda item: item[1])
    candidate_context = _missed_winner_candidate_context(candidate_rows, winner_time=winner_time)
    entry_quality = _snapshot_quality(entry, role="entry")
    winner_quality = _snapshot_quality(winner, role="winner")
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
            "outcome_basis": "trailing_stop_realized_exit",
            "realized_exit_return": max_return,
            "realized_exit_snapshot_time": realized_time,
            "observed_peak_return": observed_peak_return,
            "observed_peak_snapshot_time": observed_peak_time,
            "observed_window": {
                "snapshot_count": len(usable),
                "first_snapshot_time": _iso(entry.get("snapshot_time")),
                "last_snapshot_time": _iso(usable[-1].get("snapshot_time")),
                "winner_elapsed_days": _elapsed_days(entry.get("snapshot_time"), winner.get("snapshot_time")),
                "winner_elapsed_hours": _elapsed_hours(entry.get("snapshot_time"), winner.get("snapshot_time")),
                "source_count": len({str(row.get("data_source") or "") for row in usable if row.get("data_source")}),
                "data_sources": sorted({str(row.get("data_source") or "") for row in usable if row.get("data_source")}),
            },
            "return_path": _return_path(usable, entry_mid),
            "candidate_state": first_candidate.get("state") if first_candidate else "missing_candidate",
            "candidate_context": candidate_context,
            "entry_quality": entry_quality,
            "winner_quality": winner_quality,
            "tradability_flags": sorted(set([*entry_quality["flags"], *winner_quality["flags"]])),
            "first_snapshot": _compact_snapshot(entry),
            "winner_snapshot": _compact_snapshot(winner),
        },
    }


def _missed_winner_candidate_context(candidate_rows: list[dict[str, Any]], *, winner_time: str | None) -> dict[str, Any]:
    if not candidate_rows:
        return {
            "event_count": 0,
            "first_state": "missing_candidate",
            "last_state_before_winner": None,
            "best_state_before_winner": None,
            "first_filter_reason": "no_candidate_event",
            "hard_rejects": [],
            "blockers": [],
        }
    ordered = sorted(candidate_rows, key=lambda row: _iso(row.get("snapshot_time")))
    before_winner = [row for row in ordered if not winner_time or _iso(row.get("snapshot_time")) <= winner_time]
    first = ordered[0]
    first_raw = _json(first.get("raw"))
    hard_rejects = first_raw.get("hard_rejects") if isinstance(first_raw.get("hard_rejects"), list) else []
    blockers = first_raw.get("blockers") if isinstance(first_raw.get("blockers"), list) else []
    state_rank = {"FIRE": 0, "SETUP": 1, "WATCH": 2, "REJECT": 3}
    best = min(before_winner or ordered, key=lambda row: state_rank.get(str(row.get("state") or ""), 9))
    last = (before_winner or ordered)[-1]
    return {
        "event_count": len(ordered),
        "first_event_id": first.get("event_id"),
        "first_snapshot_time": _iso(first.get("snapshot_time")),
        "first_state": first.get("state"),
        "first_score": _number(first.get("score")),
        "first_trigger_reason": first.get("trigger_reason"),
        "first_filter_reason": _missed_filter_reason(first),
        "hard_rejects": [str(item) for item in hard_rejects if item],
        "blockers": [str(item) for item in blockers if item],
        "last_state_before_winner": last.get("state"),
        "last_score_before_winner": _number(last.get("score")),
        "best_state_before_winner": best.get("state"),
        "best_score_before_winner": _number(best.get("score")),
    }


def _snapshot_quality(row: dict[str, Any], *, role: str) -> dict[str, Any]:
    mid = _number(row.get("mid"))
    spread = _number(row.get("spread_pct"))
    volume = _number(row.get("volume"))
    open_interest = _number(row.get("open_interest"))
    flags: list[str] = []
    if mid is None or mid <= 0:
        flags.append(f"{role}_missing_mid")
    elif mid < 0.05:
        flags.append(f"{role}_penny_mid")
    if spread is None:
        flags.append(f"{role}_missing_spread")
    elif spread > 1.0:
        flags.append(f"{role}_wide_spread")
    if volume is None:
        flags.append(f"{role}_missing_volume")
    elif volume <= 0:
        flags.append(f"{role}_zero_volume")
    if open_interest is None:
        flags.append(f"{role}_missing_open_interest")
    elif open_interest < 25:
        flags.append(f"{role}_low_open_interest")
    return {
        "mid": mid,
        "spread_pct": spread,
        "volume": volume,
        "open_interest": open_interest,
        "flags": flags,
    }


def _return_path(snapshots: list[dict[str, Any]], entry_mid: float) -> list[dict[str, Any]]:
    if entry_mid <= 0:
        return []
    if len(snapshots) <= 12:
        selected = snapshots
    else:
        selected = [*snapshots[:4], *snapshots[-4:]]
        selected.extend(
            row
            for row in sorted(snapshots[4:-4], key=lambda item: (_number(item.get("mid")) or 0), reverse=True)[:4]
            if row not in selected
        )
        selected = sorted(selected, key=lambda row: _iso(row.get("snapshot_time")))
    return [
        {
            "snapshot_time": _iso(row.get("snapshot_time")),
            "mid": _number(row.get("mid")),
            "return": ((_number(row.get("mid")) or 0) / entry_mid - 1),
            "underlying_price": _number(row.get("underlying_price")),
            "spread_pct": _number(row.get("spread_pct")),
            "volume": _number(row.get("volume")),
            "open_interest": _number(row.get("open_interest")),
        }
        for row in selected
    ]
