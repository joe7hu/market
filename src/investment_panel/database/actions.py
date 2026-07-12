"""PostgreSQL mutations for journals, alerts, and strategy promotion."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.strategy_parameters import (
    EVALUABLE_GATES,
    canonical_gate_name,
    normalize_gates,
)


class ActionRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def record_trade_journal(self, *, ticker: str, contract_id: str, event_id: str | None, strategy_version: str, opportunity: dict[str, Any], notes: str) -> str:
        symbol = ticker.strip().upper()
        with self.runtime.transaction() as connection:
            instrument = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = %s", [symbol]).fetchone()
            if instrument is None:
                instrument = connection.execute(
                    "INSERT INTO catalog.instrument (symbol, name, asset_class, category) VALUES (%s, %s, 'equity', 'journal') RETURNING id",
                    [symbol, symbol],
                ).fetchone()
            decision_id = _uuid_or_none(event_id or opportunity.get("opportunity_id"))
            if decision_id is None:
                decision = connection.execute(
                    "SELECT id FROM analysis.decision WHERE instrument_id = %s ORDER BY as_of DESC LIMIT 1",
                    [instrument["id"]],
                ).fetchone()
                decision_id = decision["id"] if decision else None
            row = connection.execute(
                """
                INSERT INTO app.trade_journal (decision_id, instrument_id, action, price, rationale, details)
                VALUES (%s, %s, 'option_entry_review', %s, %s, %s) RETURNING id
                """,
                [
                    decision_id, instrument["id"], opportunity.get("premium_mid") or opportunity.get("entry_premium"), notes,
                    Jsonb({"contract_id": contract_id, "strategy_version": strategy_version, "opportunity": opportunity}),
                ],
            ).fetchone()
        return str(row["id"])

    def acknowledge_alert(self, alert_id: str) -> bool:
        with self.runtime.transaction() as connection:
            result = connection.execute(
                "UPDATE app.alert SET acknowledged_at = now() WHERE id = %s AND acknowledged_at IS NULL",
                [alert_id],
            )
        return result.rowcount == 1

    def promote_strategy_proposal(self, proposal_id: str, *, approved_by: str) -> str:
        approver = approved_by.strip()
        if not approver:
            raise ValueError("human approval is required")
        with self.runtime.transaction() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["publication:options-radar"],
            )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["strategy:options-radar-core"],
            )
            task = connection.execute(
                """
                SELECT id, result FROM analysis.agent_task
                WHERE task_kind IN ('legacy_strategy_mutation_proposal', 'strategy_mutation_proposal')
                  AND (request->>'legacy_id' = %s OR id::text = %s)
                LIMIT 1 FOR UPDATE
                """,
                [proposal_id, proposal_id],
            ).fetchone()
            if task is None:
                raise ValueError(f"strategy proposal not found: {proposal_id}")
            proposal = dict(task["result"] or {})
            proposal_status = str(proposal.get("status") or "").lower()
            if proposal_status == "backtest_required":
                raise ValueError("strategy proposal requires a passing backtest")
            if proposal_status == "forward_test_required":
                raise ValueError("strategy proposal requires a passing forward shadow test")
            if proposal_status not in {"approved", "ready", "forward_test_passed"}:
                raise ValueError("strategy proposal has not passed deterministic approval gates")
            key = str(proposal.get("proposed_strategy_version") or f"proposal-{proposal_id}")
            parameters = proposal.get("proposed_parameter_changes") or {}
            candidate_rows = connection.execute(
                "SELECT id, parameters, supersedes_id, authority_group "
                "FROM analysis.strategy_revision "
                "WHERE strategy_key = %s AND status IN ('candidate', 'testing', 'approved') "
                "ORDER BY revision DESC FOR UPDATE",
                [key],
            ).fetchall()
            requested_candidate_id = proposal.get("candidate_revision_id")
            candidate = next(
                (
                    row for row in candidate_rows
                    if (requested_candidate_id is None or int(row["id"]) == int(requested_candidate_id))
                    and _candidate_contains_changes(dict(row["parameters"] or {}), dict(parameters))
                ),
                None,
            )
            if candidate is None and not candidate_rows:
                raise ValueError("strategy proposal requires a persisted candidate revision")
            if candidate is None:
                raise ValueError("strategy proposal parameters do not match the evaluated candidate revision")
            if candidate["authority_group"] != "options-radar-core":
                raise ValueError("strategy candidate is outside the options-radar-core lineage")
            lineage = connection.execute(
                "SELECT id, status FROM analysis.strategy_revision "
                "WHERE authority_group = 'options-radar-core' FOR UPDATE"
            ).fetchall()
            statuses = {row["id"]: row["status"] for row in lineage}
            parent_id = candidate["supersedes_id"]
            if parent_id is None or statuses.get(parent_id) != "active":
                raise ValueError("strategy candidate base is no longer active; reevaluation is required")
            evaluations = connection.execute(
                "SELECT evaluation_type, verdict FROM analysis.strategy_evaluation "
                "WHERE strategy_revision_id = %s ORDER BY evaluated_at DESC",
                [candidate["id"]],
            ).fetchall()
            passed = {
                str(row["evaluation_type"]).lower()
                for row in evaluations
                if str(row["verdict"] or "").lower() in {"pass", "passed", "approved"}
            }
            if "backtest" not in passed:
                raise ValueError("strategy proposal requires a persisted passing backtest")
            if not passed.intersection({"forward_test", "forward_shadow_test"}):
                raise ValueError("strategy proposal requires a persisted passing forward shadow test")
            active_ids = [
                revision_id for revision_id, status in statuses.items()
                if status == "active" and revision_id != candidate["id"]
            ]
            if active_ids:
                connection.execute(
                    "UPDATE analysis.strategy_revision SET status = 'superseded' WHERE id = ANY(%s)",
                    [active_ids],
                )
            connection.execute(
                "UPDATE analysis.strategy_revision SET status = 'active', promoted_at = now() WHERE id = %s",
                [candidate["id"]],
            )
            connection.execute(
                "UPDATE app.publication SET status = 'superseded' "
                "WHERE scope = 'options-radar' AND status = 'published'"
            )
            connection.execute(
                "UPDATE analysis.agent_task SET validation = %s, updated_at = now() WHERE id = %s",
                [Jsonb({"status": "promoted", "approved_by": approver}), task["id"]],
            )
        return key


def _uuid_or_none(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _candidate_contains_changes(candidate: dict[str, Any], changes: dict[str, Any]) -> bool:
    gates = normalize_gates(candidate)
    for key, value in changes.items():
        canonical = canonical_gate_name(key)
        actual = gates.get(canonical) if canonical in EVALUABLE_GATES else candidate.get(key)
        if actual != value:
            return False
    return True
