"""PostgreSQL mutations for journals, alerts, and strategy promotion."""

from __future__ import annotations

from datetime import UTC, datetime
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

    def record_trade_journal(
        self,
        *,
        ticker: str,
        contract_id: str,
        event_id: str | None,
        strategy_version: str,
        opportunity: dict[str, Any],
        notes: str,
        action: str = "accepted",
        idempotency_key: str | None = None,
        publication_id: str | None = None,
        expected_contract_version: int | None = None,
    ) -> str:
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
            if expected_contract_version is not None and expected_contract_version != 3:
                raise ValueError("stale options-radar contract version")
            if idempotency_key:
                prior = connection.execute(
                    "SELECT id FROM app.trade_journal WHERE details->>'idempotency_key' = %s LIMIT 1",
                    [idempotency_key],
                ).fetchone()
                if prior:
                    return str(prior["id"])
            row = connection.execute(
                """
                INSERT INTO app.trade_journal (decision_id, instrument_id, action, price, rationale, details)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                """,
                [
                    decision_id, instrument["id"], action,
                    opportunity.get("entry_price") or opportunity.get("premium_mid") or opportunity.get("entry_premium"), notes,
                    Jsonb({
                        "contract_id": contract_id,
                        "strategy_version": strategy_version,
                        "publication_id": publication_id,
                        "contract_version": expected_contract_version,
                        "idempotency_key": idempotency_key,
                        "opportunity": opportunity,
                    }),
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

    def stage_option_paper_entry(
        self,
        *,
        decision_id: UUID,
        idempotency_key: str,
        expected_contract_version: int,
        limit_price: float | None,
    ) -> dict[str, Any]:
        if expected_contract_version != 3:
            raise ValueError("stale options-radar contract version")
        key = idempotency_key.strip()
        if not key:
            raise ValueError("idempotency key is required")
        with self.runtime.transaction() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["paper-order:options-radar"],
            )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["publication:options-radar"],
            )
            prior = connection.execute(
                "SELECT id, status, reserved_collateral FROM app.paper_order WHERE idempotency_key = %s",
                [key],
            ).fetchone()
            if prior:
                return {
                    "status": str(prior["status"]),
                    "paper_order_id": str(prior["id"]),
                    "reserved_collateral": float(prior["reserved_collateral"] or 0),
                    "idempotent_replay": True,
                }
            signal = connection.execute(
                """
                SELECT decision.instrument_id, decision.state, option_decision.structure,
                       option_decision.entry_price, option_decision.secured_cash,
                       option_decision.max_loss, option_decision.details,
                       (
                           SELECT item.payload FROM app.publication publication
                           JOIN app.publication_item item ON item.publication_id = publication.id
                           WHERE publication.scope = 'options-radar'
                             AND publication.status = 'published'
                             AND item.model_name = 'option_radar_opportunity'
                             AND item.payload->>'decision_id' = decision.id::text
                           LIMIT 1
                       ) AS publication_payload,
                       EXISTS (
                           SELECT 1 FROM app.publication publication
                           JOIN app.publication_item item ON item.publication_id = publication.id
                           WHERE publication.scope = 'options-radar'
                             AND publication.status = 'published'
                             AND item.model_name = 'option_radar_opportunity'
                             AND item.payload->>'decision_id' = decision.id::text
                             AND item.payload->>'execution_ready' = 'true'
                       ) AS currently_published
                FROM analysis.decision decision
                JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
                WHERE decision.id = %s FOR UPDATE
                """,
                [decision_id],
            ).fetchone()
            if signal is None:
                raise ValueError("options-radar signal not found")
            if not signal["currently_published"]:
                raise ValueError("signal is stale or not execution-ready in the current publication")
            if str(signal["state"]) != "READY":
                raise ValueError("signal decision state is not READY")
            from investment_panel.database.options_publication import _contract_readiness

            publication_payload = dict(signal["publication_payload"] or {})
            if _contract_readiness(publication_payload, datetime.now(UTC)) != "A":
                raise ValueError("signal quote is no longer execution-grade")
            structure = str(signal["structure"] or "long_option")
            collateral = float(signal["secured_cash"] or 0)
            account = connection.execute(
                "SELECT net_liquidation, cash_balance, buying_power, observed_at "
                "FROM raw.broker_account_snapshot ORDER BY observed_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if structure == "cash_secured_put":
                if account is None or account["net_liquidation"] is None or account["cash_balance"] is None:
                    raise ValueError("current broker cash and NAV are required for a cash-secured put")
                nav = float(account["net_liquidation"])
                available_cash = min(float(account["cash_balance"]), float(account["buying_power"] or account["cash_balance"]))
                reserved = float(
                    connection.execute(
                        "SELECT COALESCE(sum(reserved_collateral), 0) AS total FROM app.paper_order "
                        "WHERE structure = 'cash_secured_put' AND status IN ('staged', 'open', 'entered')"
                    ).fetchone()["total"]
                )
                if collateral <= 0:
                    raise ValueError("cash-secured-put collateral is unavailable")
                if collateral > nav * 0.05:
                    raise ValueError("one contract exceeds the 5% NAV ticker limit")
                if reserved + collateral > nav * 0.15:
                    raise ValueError("aggregate cash-secured-put collateral would exceed 15% NAV")
                if reserved + collateral > available_cash:
                    raise ValueError("insufficient unreserved cash collateral")
            quantity = 1
            side = "sell" if structure == "cash_secured_put" else "buy"
            policy = {
                "contract_version": 3,
                "structure": structure,
                "fully_cash_secured": structure == "cash_secured_put",
                "live_order_submission": False,
            }
            row = connection.execute(
                """
                INSERT INTO app.paper_order
                    (decision_id, instrument_id, side, quantity, limit_price, status,
                     policy_result, structure, reserved_collateral, idempotency_key)
                VALUES (%s, %s, %s, %s, %s, 'staged', %s, %s, %s, %s)
                RETURNING id
                """,
                [
                    decision_id, signal["instrument_id"], side, quantity,
                    limit_price if limit_price is not None else signal["entry_price"],
                    Jsonb(policy), structure, collateral or None, key,
                ],
            ).fetchone()
        return {
            "status": "staged",
            "paper_order_id": str(row["id"]),
            "decision_id": str(decision_id),
            "structure": structure,
            "reserved_collateral": collateral,
            "live_order_submission": False,
            "idempotent_replay": False,
        }

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
