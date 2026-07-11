"""PostgreSQL mutations for journals, alerts, and strategy promotion."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import DatabaseRuntime


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
            current = connection.execute(
                "SELECT coalesce(max(revision), 0) AS revision FROM analysis.strategy_revision WHERE strategy_key = %s",
                [key],
            ).fetchone()
            revision = int(current["revision"]) + 1
        AnalysisRepository(self.runtime).register_strategy(key, revision, name=key, status="active", parameters=dict(parameters))
        with self.runtime.transaction() as connection:
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
