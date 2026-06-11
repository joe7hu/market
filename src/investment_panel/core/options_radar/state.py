"""Radar state-transition tracking."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.source_ingestion.utils import (stable_id)
from investment_panel.core.options_radar.coerce import (_integer, _iso, _json, _normalize_symbol, _number)
from investment_panel.core.options_radar.constants import (DEFAULT_STRATEGY_VERSION)
from investment_panel.core.options_radar.shadow import (_latest_legacy_thesis_validation_by_ticker, _latest_thesis_validation_by_candidate_event, _mark_for_snapshot, _shadow_marks_by_trade, _shadow_trades_by_contract, _thesis_exit_reason, _trade_for_snapshot)

def refresh_radar_state_transitions(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    candidates = query_rows(
        con,
        """
        SELECT *
        FROM candidate_event
        WHERE strategy_version = ?
        ORDER BY ticker, contract_id, snapshot_time
        """,
        [strategy_version],
    )
    con.execute("DELETE FROM radar_state_transition WHERE strategy_version = ?", [strategy_version])
    trades_by_contract = _shadow_trades_by_contract(con, strategy_version)
    marks_by_trade = _shadow_marks_by_trade(con, strategy_version)
    thesis_validation_by_candidate_event = _latest_thesis_validation_by_candidate_event(con, strategy_version)
    legacy_thesis_validation_by_ticker = _latest_legacy_thesis_validation_by_ticker(con, strategy_version)
    evaluated_at = datetime.utcnow().isoformat()
    previous_by_contract: dict[str, str] = {}
    count = 0
    for candidate in candidates:
        contract_id = str(candidate.get("contract_id") or "")
        snapshot_time = _iso(candidate.get("snapshot_time"))
        trade = _trade_for_snapshot(trades_by_contract.get(contract_id, []), snapshot_time)
        mark = _mark_for_snapshot(marks_by_trade.get(str((trade or {}).get("trade_id") or ""), []), snapshot_time) if trade else None
        thesis_validation = thesis_validation_by_candidate_event.get(str(candidate.get("event_id") or ""))
        if not thesis_validation:
            thesis_validation = legacy_thesis_validation_by_ticker.get(_normalize_symbol(candidate.get("ticker")))
        state = build_radar_state(candidate, trade, mark, thesis_validation)
        previous_state = previous_by_contract.get(contract_id)
        if previous_state == state["state"]:
            continue
        transition = {
            **state,
            "transition_id": stable_id("radar_state_transition", strategy_version, contract_id, snapshot_time, previous_state, state["state"]),
            "evaluated_at": evaluated_at,
            "snapshot_time": snapshot_time,
            "ticker": _normalize_symbol(candidate.get("ticker")),
            "contract_id": contract_id,
            "strategy_version": strategy_version,
            "previous_state": previous_state,
            "candidate_state": str(candidate.get("state") or "").upper(),
            "event_id": candidate.get("event_id"),
            "trade_id": (trade or {}).get("trade_id"),
            "mark_id": (mark or {}).get("mark_id"),
            "thesis_id": candidate.get("thesis_id"),
        }
        con.execute(
            """
            INSERT OR REPLACE INTO radar_state_transition
            (transition_id, evaluated_at, snapshot_time, ticker, contract_id,
             strategy_version, previous_state, state, candidate_state, event_id,
             trade_id, mark_id, thesis_id, trigger_reason, evidence_refs, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                transition["transition_id"],
                transition["evaluated_at"],
                transition["snapshot_time"],
                transition["ticker"],
                transition["contract_id"],
                transition["strategy_version"],
                transition["previous_state"],
                transition["state"],
                transition["candidate_state"],
                transition["event_id"],
                transition["trade_id"],
                transition["mark_id"],
                transition["thesis_id"],
                transition["trigger_reason"],
                json_dumps(transition["evidence_refs"]),
                json_dumps(transition["raw"]),
            ],
        )
        previous_by_contract[contract_id] = transition["state"]
        count += 1
    return count


def build_radar_state(
    candidate: dict[str, Any],
    trade: dict[str, Any] | None,
    mark: dict[str, Any] | None,
    thesis_validation: dict[str, Any] | None,
) -> dict[str, Any]:
    candidate_state = str(candidate.get("state") or "WATCH").upper()
    evidence_refs = [{"type": "candidate_event", "id": candidate.get("event_id")}]
    raw_candidate = _json(candidate.get("raw"))
    raw: dict[str, Any] = {
        "authority": "deterministic_state_transition",
        "candidate_state": candidate_state,
        "candidate_blockers": raw_candidate.get("blockers") or [],
        "candidate_hard_rejects": raw_candidate.get("hard_rejects") or [],
    }
    if thesis_validation:
        evidence_refs.append({"type": "agent_thesis_validation", "id": thesis_validation.get("validation_id")})
        raw.update(
            {
                "thesis_validation_state": str(thesis_validation.get("state") or "").lower() or None,
                "thesis_invalidation_status": thesis_validation.get("invalidation_status"),
                "thesis_red_team_status": thesis_validation.get("red_team_status"),
                "thesis_proof_status": thesis_validation.get("proof_status"),
                "thesis_catalyst_status": thesis_validation.get("catalyst_status"),
            }
        )
    thesis_exit_reason = _thesis_exit_reason(thesis_validation)
    if not trade:
        if thesis_exit_reason:
            return {
                "state": "INVALIDATED",
                "trigger_reason": thesis_exit_reason,
                "evidence_refs": evidence_refs,
                "raw": raw,
            }
        return {
            "state": candidate_state,
            "trigger_reason": str(candidate.get("trigger_reason") or candidate_state.lower()),
            "evidence_refs": evidence_refs,
            "raw": raw,
        }

    evidence_refs.append({"type": "shadow_trade", "id": trade.get("trade_id")})
    entry_time = _iso(trade.get("entry_time"))
    snapshot_time = _iso(candidate.get("snapshot_time"))
    if mark:
        evidence_refs.append({"type": "shadow_trade_mark", "id": mark.get("mark_id")})
    validation_state = str((thesis_validation or {}).get("state") or "").lower()
    current_return = _number((mark or {}).get("current_return"))
    max_drawdown = _number((mark or {}).get("max_drawdown_since_alert"))
    dte = _integer((mark or {}).get("dte"))
    raw.update(
        {
            "trade_id": trade.get("trade_id"),
            "mark_id": (mark or {}).get("mark_id"),
            "current_return": current_return,
            "max_drawdown_since_alert": max_drawdown,
            "dte": dte,
            "thesis_validation_state": validation_state or None,
            "exit_loss_threshold": -0.60,
            "trim_return_threshold": 4.0,
        }
    )
    if snapshot_time == entry_time:
        return {"state": "FIRE", "trigger_reason": "premium_triggered_shadow_entry", "evidence_refs": evidence_refs, "raw": raw}
    if thesis_exit_reason:
        return {"state": "INVALIDATED", "trigger_reason": thesis_exit_reason, "evidence_refs": evidence_refs, "raw": raw}
    if current_return is not None and current_return <= -0.60:
        return {"state": "EXIT", "trigger_reason": "option_loss_60pct", "evidence_refs": evidence_refs, "raw": raw}
    if max_drawdown is not None and max_drawdown <= -0.60:
        return {"state": "EXIT", "trigger_reason": "max_drawdown_60pct", "evidence_refs": evidence_refs, "raw": raw}
    if dte is not None and dte <= 30:
        return {"state": "EXIT", "trigger_reason": "near_expiry", "evidence_refs": evidence_refs, "raw": raw}
    if (mark or {}).get("time_to_10x") is not None or (current_return is not None and current_return >= 9.0):
        return {"state": "TRIM", "trigger_reason": "hit_10x", "evidence_refs": evidence_refs, "raw": raw}
    if (mark or {}).get("time_to_5x") is not None or (current_return is not None and current_return >= 4.0):
        return {"state": "TRIM", "trigger_reason": "hit_5x", "evidence_refs": evidence_refs, "raw": raw}
    if (mark or {}).get("time_to_2x") is not None or (current_return is not None and current_return >= 1.0):
        return {"state": "HOLD", "trigger_reason": "hit_2x_continue_tracking", "evidence_refs": evidence_refs, "raw": raw}
    return {"state": "HOLD", "trigger_reason": "shadow_trade_still_validating", "evidence_refs": evidence_refs, "raw": raw}
