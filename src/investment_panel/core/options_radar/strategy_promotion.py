"""Proposal gate evaluation and human-approved promotion."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.options_radar.coerce import (_json)
from investment_panel.core.options_radar.constants import (DEFAULT_STRATEGY_VERSION)
from investment_panel.core.options_radar.dbutil import (_strategy_parameters)
from investment_panel.core.options_radar.strategy_backtest import (build_strategy_backtest_result, build_strategy_forward_test_result, insert_strategy_backtest_result, insert_strategy_forward_test_result)
from investment_panel.core.options_radar.strategy_common import (_strategy_proposal_is_terminal)
from investment_panel.core.options_radar.strategy_outcomes import (_proposed_strategy_parameters)

def refresh_strategy_proposal_evaluations(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> dict[str, int]:
    proposals = query_rows(
        con,
        """
        SELECT *
        FROM strategy_mutation_proposal
        WHERE strategy_version = ?
        ORDER BY created_at DESC
        """,
        [strategy_version],
    )
    backtests = 0
    forward_tests = 0
    updates = 0
    for proposal in proposals:
        backtest = build_strategy_backtest_result(con, proposal)
        if backtest:
            insert_strategy_backtest_result(con, backtest)
            backtests += 1
        forward = build_strategy_forward_test_result(con, proposal)
        if forward:
            insert_strategy_forward_test_result(con, forward)
            forward_tests += 1
        updates += update_strategy_proposal_gate_status(con, proposal["proposal_id"])
    return {"strategy_backtests": backtests, "strategy_forward_tests": forward_tests, "strategy_gate_updates": updates}


def update_strategy_proposal_gate_status(con: Any, proposal_id: str) -> int:
    before = query_rows(
        con,
        "SELECT status, human_approval_status FROM strategy_mutation_proposal WHERE proposal_id = ?",
        [proposal_id],
    )
    if not before or _strategy_proposal_is_terminal(before[0]):
        return 0
    backtest = _latest_backtest(con, proposal_id)
    forward = _latest_forward_test(con, proposal_id)
    if not backtest:
        status = "backtest_required"
    elif backtest.get("verdict") != "pass":
        status = "backtest_failed"
    elif not forward or forward.get("verdict") == "collecting_data":
        status = "forward_test_required"
    elif forward.get("verdict") != "pass":
        status = "forward_test_failed"
    else:
        status = "ready_for_human_review"
    if before[0].get("status") == status:
        return 0
    con.execute("UPDATE strategy_mutation_proposal SET status = ? WHERE proposal_id = ?", [status, proposal_id])
    return 1


def promote_strategy_mutation(con: Any, proposal_id: str, *, approved_by: str | None = None) -> str:
    if not approved_by:
        raise StrategyPromotionError("human approval is required before promotion")
    proposal_rows = query_rows(con, "SELECT * FROM strategy_mutation_proposal WHERE proposal_id = ?", [proposal_id])
    if not proposal_rows:
        raise StrategyPromotionError(f"unknown strategy proposal: {proposal_id}")
    proposal = proposal_rows[0]
    backtest = _latest_backtest(con, proposal_id)
    forward = _latest_forward_test(con, proposal_id)
    if not backtest or backtest.get("verdict") != "pass":
        raise StrategyPromotionError("passing backtest is required before promotion")
    if not forward or forward.get("verdict") != "pass":
        raise StrategyPromotionError("passing forward shadow test is required before promotion")
    base_params = _strategy_parameters(con, proposal["strategy_version"])
    proposed_params = _proposed_strategy_parameters(base_params, proposal.get("proposed_parameter_changes"))
    promoted_at = datetime.utcnow().isoformat()
    con.execute(
        """
        INSERT OR REPLACE INTO option_strategy_versions
        (strategy_version, strategy_name, version, created_at, status,
         parameters, promoted_at, supersedes, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            proposal["proposed_strategy_version"],
            proposed_params.get("strategy_name") or proposal["proposed_strategy_version"],
            int(base_params.get("version") or 1) + 1,
            promoted_at,
            "promoted",
            json_dumps(proposed_params),
            promoted_at,
            proposal["strategy_version"],
            f"Promoted from {proposal_id} after deterministic backtest, forward test, and human approval by {approved_by}.",
        ],
    )
    con.execute(
        """
        UPDATE strategy_mutation_proposal
        SET status = 'promoted',
            human_approval_status = 'approved',
            approved_by = ?,
            approved_at = ?,
            raw = ?
        WHERE proposal_id = ?
        """,
        [
            approved_by,
            promoted_at,
            json_dumps({**_json(proposal.get("raw")), "approved_by": approved_by, "approved_at": promoted_at}),
            proposal_id,
        ],
    )
    return str(proposal["proposed_strategy_version"])


def _latest_backtest(con: Any, proposal_id: str) -> dict[str, Any] | None:
    rows = query_rows(
        con,
        """
        SELECT *
        FROM strategy_backtest_result
        WHERE proposal_id = ?
        ORDER BY evaluated_at DESC
        LIMIT 1
        """,
        [proposal_id],
    )
    return rows[0] if rows else None


def _latest_forward_test(con: Any, proposal_id: str) -> dict[str, Any] | None:
    rows = query_rows(
        con,
        """
        SELECT *
        FROM strategy_forward_test_result
        WHERE proposal_id = ?
        ORDER BY evaluated_at DESC
        LIMIT 1
        """,
        [proposal_id],
    )
    return rows[0] if rows else None


class StrategyPromotionError(ValueError):
    """Raised when a strategy proposal is not eligible for promotion."""
