"""Ordered next-action policy for QQQ options underwriting."""

from __future__ import annotations

from typing import Any

from investment_panel.core.option_underwriting import thesis_blocker


def next_required_action(
    analysis: dict[str, int],
    thesis: dict[str, Any],
    canary: dict[str, Any],
) -> str:
    if int(canary["post_fix_complete_captures"]) == 0:
        return "collect_post_fix_complete_capture"
    if analysis["eligible_groups"] == 0:
        return "restore_eligible_fresh_quote_groups"
    active_thesis_blocker = thesis_blocker(thesis)
    if active_thesis_blocker == "thesis_direction_required" and thesis:
        return "wait_for_directional_qqq_thesis"
    if active_thesis_blocker is not None:
        return "run_qqq_thesis_monitor"
    if int(canary["qualified_regular_sessions"]) < int(canary["required_regular_sessions"]):
        return "complete_five_qualified_post_fix_sessions"
    return "collect_exact_structure_mature_outcomes"
