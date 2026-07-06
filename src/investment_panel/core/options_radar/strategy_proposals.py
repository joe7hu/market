"""Strategy mutation proposals from missed winners."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.source_ingestion.utils import (stable_id)
from investment_panel.core.options_radar.coerce import (_list_value, _number)
from investment_panel.core.options_radar.constants import (DEFAULT_STRATEGY_VERSION)
from investment_panel.core.options_radar.strategy_common import (_proposal_parameter_changes, _strategy_proposal_is_terminal)

VERSION_SUFFIX_EXCLUDED_CHANGE_KEYS = {"candidate_note", "strategy_name", "strategy_family"}

def generate_strategy_mutation_proposals(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    rows = query_rows(
        con,
        """
        SELECT filter_reason, proposed_strategy_family, count(*) AS missed_count,
               max(max_return_seen) AS best_return,
               list(missed_id) AS missed_ids
        FROM missed_winner_event
        WHERE strategy_version = ?
        GROUP BY filter_reason, proposed_strategy_family
        ORDER BY missed_count DESC, best_return DESC
        """,
        [strategy_version],
    )
    count = 0
    for row in rows:
        proposal = build_strategy_mutation_proposal(row, strategy_version)
        if not proposal:
            continue
        existing_rows = query_rows(
            con,
            "SELECT status, human_approval_status FROM strategy_mutation_proposal WHERE proposal_id = ?",
            [proposal["proposal_id"]],
        )
        if existing_rows and _strategy_proposal_is_terminal(existing_rows[0]):
            continue
        before = 1 if existing_rows else 0
        con.execute(
            """
            INSERT OR REPLACE INTO strategy_mutation_proposal
            (proposal_id, created_at, source_type, strategy_version, proposed_strategy_version,
             proposed_parameter_changes, rationale, expected_effect, risk, status,
             requires_backtest, requires_forward_test, human_approval_status,
             evidence_refs, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                proposal["proposal_id"],
                proposal["created_at"],
                proposal["source_type"],
                proposal["strategy_version"],
                proposal["proposed_strategy_version"],
                json_dumps(proposal["proposed_parameter_changes"]),
                proposal["rationale"],
                proposal["expected_effect"],
                proposal["risk"],
                proposal["status"],
                proposal["requires_backtest"],
                proposal["requires_forward_test"],
                proposal["human_approval_status"],
                json_dumps(proposal["evidence_refs"]),
                json_dumps(proposal["raw"]),
            ],
        )
        after = query_rows(con, "SELECT count(*) AS count FROM strategy_mutation_proposal WHERE proposal_id = ?", [proposal["proposal_id"]])[0]["count"]
        count += int(after) - int(before)
    return count


def build_strategy_mutation_proposal(row: dict[str, Any], strategy_version: str) -> dict[str, Any] | None:
    filter_reason = str(row.get("filter_reason") or "unknown")
    family = str(row.get("proposed_strategy_family") or "leap_10x_variant")
    changes = _proposal_parameter_changes(filter_reason, option_type=_option_type_for_family(family))
    if not changes:
        return None
    # Key the promotable version on the actual parameter delta, not just the family.
    # Several distinct filter reasons map to the same family (e.g. open_interest / volume
    # / spread -> leap_10x_liquidity_watch) with *different* loosenings; a flat
    # "{family}_proposed_v1" made them all promote into one version string, so the last
    # promotion silently overwrote the others' parameters. The change-set suffix gives
    # each distinct loosening its own version while collapsing genuinely identical ones.
    effective_changes = {key: value for key, value in changes.items() if key not in VERSION_SUFFIX_EXCLUDED_CHANGE_KEYS}
    variant_suffix = "_".join(sorted(effective_changes)) or "variant"
    proposed_version = f"{family}__{variant_suffix}"
    missed_count = int(row.get("missed_count") or 0)
    best_return = _number(row.get("best_return")) or 0.0
    missed_ids = _list_value(row.get("missed_ids"))
    return {
        "proposal_id": stable_id("strategy_mutation_proposal", strategy_version, filter_reason, family),
        "created_at": datetime.utcnow().isoformat(),
        "source_type": "deterministic_missed_winner_analysis",
        "strategy_version": strategy_version,
        "proposed_strategy_version": proposed_version,
        "proposed_parameter_changes": changes,
        "rationale": f"{missed_count} missed winner(s) were filtered by {filter_reason}; best observed return was {best_return + 1:.2f}x.",
        "expected_effect": "Increase recall for similar 5x/10x contracts in shadow mode.",
        "risk": "May increase false positives or earlier entries; must pass deterministic backtest and forward shadow comparison before promotion.",
        "status": "proposed",
        "requires_backtest": True,
        "requires_forward_test": True,
        "human_approval_status": "required",
        "evidence_refs": [{"type": "missed_winner_event", "id": missed_id} for missed_id in missed_ids],
        "raw": {
            "filter_reason": filter_reason,
            "missed_count": missed_count,
            "best_return": best_return,
            "promotion_policy": "no_auto_promotion",
        },
    }


def _option_type_for_family(family: str) -> str | None:
    if family == "short_dated_lottery_call_spread":
        return "call_spread"
    if family == "short_dated_lottery_call":
        return "call"
    return None
