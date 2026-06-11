"""Shared strategy-lab helpers (proposal mapping, attribution labels, terminal-state)."""

from __future__ import annotations

from typing import Any

from investment_panel.core.db import (query_rows)
from investment_panel.core.options_radar.coerce import (_json)

def _proposed_family(filter_reason: str) -> str:
    if "delta" in filter_reason:
        return "leap_10x_momentum_lottery"
    if "iv" in filter_reason:
        return "leap_10x_high_iv_catalyst"
    if "open_interest" in filter_reason or "volume" in filter_reason or "spread" in filter_reason:
        return "leap_10x_liquidity_watch"
    if "50d" in filter_reason or "rs_vs_qqq" in filter_reason:
        return "leap_10x_early_reversal"
    return "leap_10x_variant"


def _proposal_parameter_changes(filter_reason: str) -> dict[str, Any]:
    if "delta_outside_strategy_range" in filter_reason:
        return {"delta_min": 0.10, "delta_max": 0.45, "candidate_note": "test lower-delta lottery sleeve separately"}
    if "iv_percentile" in filter_reason:
        return {"max_iv_percentile": 85.0, "candidate_note": "test high-IV catalyst sleeve separately"}
    if "open_interest" in filter_reason:
        return {"min_open_interest": 25, "candidate_note": "test low-OI contracts only with stronger spread and volume gates"}
    if "volume" in filter_reason:
        return {"min_volume": 0, "candidate_note": "test no-volume LEAP snapshots with stricter OI and spread gates"}
    if "spread" in filter_reason:
        return {"max_spread_pct": 0.35, "candidate_note": "test wider spreads only in shadow mode"}
    if "50d" in filter_reason:
        return {"require_price_above_ma50": False, "candidate_note": "test pre-50D early reversal sleeve"}
    if "rs_vs_qqq" in filter_reason:
        return {"require_rs_improving": False, "candidate_note": "test pre-RS recovery sleeve"}
    if "required_move" in filter_reason:
        return {"max_required_move_pct": 5.0, "candidate_note": "test larger required moves as a separate lottery strategy"}
    return {}


def _missed_filter_reason(candidate: dict[str, Any] | None) -> str:
    if not candidate:
        return "no_candidate_event"
    raw = _json(candidate.get("raw"))
    hard_rejects = raw.get("hard_rejects") if isinstance(raw.get("hard_rejects"), list) else []
    blockers = raw.get("blockers") if isinstance(raw.get("blockers"), list) else []
    reasons = [str(item) for item in [*hard_rejects, *blockers] if item]
    return reasons[0] if reasons else str(candidate.get("state") or "unknown_filter")


def _strategy_proposal_is_terminal(proposal: dict[str, Any]) -> bool:
    status = str(proposal.get("status") or "").lower()
    human_status = str(proposal.get("human_approval_status") or "").lower()
    return status in {"promoted", "rejected"} or human_status in {"approved", "rejected"}


def _latest_attribution_labels(con: Any) -> dict[str, str]:
    rows = query_rows(
        con,
        """
        WITH candidate_labels AS (
            SELECT event_id, label
            FROM candidate_event_attribution
            QUALIFY row_number() OVER (PARTITION BY event_id ORDER BY snapshot_time DESC) = 1
        ),
        shadow_labels AS (
            SELECT event_id, label
            FROM option_attribution
            QUALIFY row_number() OVER (PARTITION BY event_id ORDER BY snapshot_time DESC) = 1
        )
        SELECT event_id, label
        FROM candidate_labels
        UNION ALL
        SELECT s.event_id, s.label
        FROM shadow_labels s
        LEFT JOIN candidate_labels c ON c.event_id = s.event_id
        WHERE c.event_id IS NULL
        """,
    )
    return {str(row["event_id"]): str(row["label"]) for row in rows if row.get("event_id") and row.get("label")}


def _attribution_label(
    option_return: float,
    underlying_return: float | None,
    iv_change: float | None,
    theta_effect: float,
    spread_change: float | None,
) -> str:
    if spread_change is not None and spread_change > 0.10:
        return "liquidity_risk"
    if underlying_return is not None and underlying_return > 0.02 and option_return > 0.10:
        return "good_convexity"
    if underlying_return is not None and underlying_return > 0.02 and option_return <= 0.0:
        return "iv_crush_or_bad_strike"
    if underlying_return is not None and abs(underlying_return) <= 0.01 and option_return < 0.0:
        return "theta_iv_bleed"
    if iv_change is not None and iv_change < -0.05 and option_return < 0.0:
        return "iv_crush"
    if theta_effect < -0.05 and option_return < 0.0:
        return "theta_decay"
    return "mixed"
