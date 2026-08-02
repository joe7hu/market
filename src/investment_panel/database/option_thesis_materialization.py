"""Materialize accepted option-agent hypotheses into canonical research state.

The agent proposes a falsifiable thesis and an allowed expression envelope.
This module owns the single PostgreSQL seam that turns that proposal into a
versioned thesis plus a research-only expression.  It never selects contracts,
sizes risk, promotes readiness, or stages an order.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.database.agent_process import jsonable
from investment_panel.database.instruments import canonical_symbol, reconcile_instrument
from investment_panel.database.thesis import normalize_thesis_v3


def materialize_option_thesis(
    connection: Any,
    *,
    task_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    task = connection.execute(
        """
        SELECT id, decision_id, agent_run_id, request
        FROM analysis.agent_task
        WHERE id = %s AND task_kind = 'option_thesis'
        FOR UPDATE
        """,
        [task_id],
    ).fetchone()
    if task is None:
        raise ValueError(f"option thesis task not found: {task_id}")

    request = dict(task["request"] or {})
    symbol = canonical_symbol(str(result.get("ticker") or request.get("ticker") or ""))
    requested_symbol = canonical_symbol(str(request.get("ticker") or symbol))
    if symbol != requested_symbol:
        raise ValueError(f"agent thesis ticker mismatch: expected {requested_symbol}, received {symbol}")

    instrument_id = reconcile_instrument(connection, symbol, name=symbol, category="option-agent")
    current = connection.execute(
        """
        SELECT id, revision, thesis
        FROM app.thesis
        WHERE instrument_id = %s AND status = 'current'
        ORDER BY revision DESC LIMIT 1
        FOR UPDATE
        """,
        [instrument_id],
    ).fetchone()
    previous = dict(current["thesis"] or {}) if current else {}
    previous_provenance = dict(previous.get("provenance") or {})
    if str(previous_provenance.get("option_agent_task_id") or "") == str(task_id):
        expression = connection.execute(
            "SELECT id FROM app.thesis_expression WHERE thesis_revision_id = %s AND status = 'active' ORDER BY id DESC LIMIT 1",
            [current["id"]],
        ).fetchone()
        return {
            "status": "already_materialized",
            "thesis_revision_id": int(current["id"]),
            "expression_id": int(expression["id"]) if expression else None,
        }
    if str(previous.get("automation_policy") or "").lower() == "manual_lock":
        return {"status": "blocked_manual_lock", "thesis_revision_id": int(current["id"])}

    confidence_number = float(result.get("confidence") or 0)
    confidence = "high" if confidence_number >= 0.75 else "medium" if confidence_number >= 0.5 else "low"
    probabilities = dict(result["scenario_probabilities"])
    evidence_refs = [_evidence_reference(item) for item in result.get("evidence_refs") or []]
    evidence_refs = [item for item in evidence_refs if item]
    decision_id = str(task["decision_id"] or request.get("decision_id") or "") or None
    agent_run_id = str(task["agent_run_id"] or "") or None
    invalidation_rules = [
        {
            "id": f"option-agent-{index}",
            "type": "event",
            "operator": "occurs",
            "text": str(text).strip(),
        }
        for index, text in enumerate(result.get("invalidation") or [], start=1)
        if str(text).strip()
    ]
    fields = {
        "core_thesis": str(result["core_thesis"]).strip(),
        "why_owned_watched": "Option-radar hypothesis; deterministic ticket policy retains contract, sizing, readiness, and staging authority.",
        "direction": str(result["direction"]).lower(),
        "timeframe": f"through {result['bull_target_date']}",
        "horizon_date": str(result["bull_target_date"]),
        "conviction": confidence,
        "confidence": confidence,
        "pillars": [
            {
                "id": f"required-proof-{index}",
                "title": "Required proof",
                "claim": str(proof).strip(),
                "evidence_refs": evidence_refs,
            }
            for index, proof in enumerate(result.get("required_proofs") or [], start=1)
            if str(proof).strip()
        ],
        "scenarios": {
            "base": {
                "probability": float(probabilities["base"]),
                "target": float(result["base_target_price"]),
                "rationale": "Agent base case; requires deterministic evidence validation.",
            },
            "bull": {
                "probability": float(probabilities["bull"]),
                "target": float(result["bull_target_price"]),
                "rationale": str(result["core_thesis"]).strip(),
            },
            "bear": {
                "probability": float(probabilities["bear"]),
                "target": float(result["bear_target_price"]),
                "rationale": str(result["bear_case"]).strip(),
            },
        },
        "catalysts": list(result.get("catalysts") or []),
        "invalidation_rules": invalidation_rules,
        "lifecycle_status": "active",
        "evidence_coverage_status": "partial" if evidence_refs else "low",
        "automation_policy": "auto",
        "evidence_links": evidence_refs,
        "provenance": {
            **previous_provenance,
            "source": "option_agent",
            "authority": "hypothesis_only",
            "option_agent_task_id": str(task_id),
            "option_agent_run_id": agent_run_id,
            "decision_id": decision_id,
            "evidence_refs": evidence_refs,
        },
        "author_kind": "ai",
    }
    thesis = normalize_thesis_v3(fields, previous=previous, symbol=symbol)
    revision = int(current["revision"]) + 1 if current else 1
    superseded_id = int(current["id"]) if current else None
    if current:
        connection.execute(
            "UPDATE app.thesis SET status = 'superseded', updated_at = now() WHERE id = %s",
            [current["id"]],
        )
        connection.execute(
            "UPDATE app.thesis_expression SET status = 'superseded', updated_at = now() WHERE thesis_revision_id = %s AND status = 'active'",
            [current["id"]],
        )
    inserted = connection.execute(
        """
        INSERT INTO app.thesis (
            instrument_id, revision, status, thesis, schema_version, author_kind,
            superseded_revision_id, change_rationale, last_assessed_at
        )
        VALUES (%s, %s, 'current', %s, 3, 'ai', %s, %s, now())
        RETURNING id
        """,
        [
            instrument_id,
            revision,
            Jsonb(thesis),
            superseded_id,
            f"Accepted option-agent hypothesis for decision {decision_id or 'unscoped'}; research authority only.",
        ],
    ).fetchone()
    expression = connection.execute(
        """
        INSERT INTO app.thesis_expression (
            instrument_id, thesis_revision_id, expression_kind, structure,
            entry_logic, horizon_date, invalidation_rules, status
        )
        VALUES (%s, %s, 'option', %s, %s, %s, %s, 'active')
        RETURNING id
        """,
        [
            instrument_id,
            inserted["id"],
            Jsonb({
                "direction": str(result["direction"]).lower(),
                "preferred_structures": list(result.get("preferred_structures") or []),
                "scenario_targets": {
                    "base": float(result["base_target_price"]),
                    "bull": float(result["bull_target_price"]),
                    "bear": float(result["bear_target_price"]),
                },
                "scenario_probabilities": {key: float(probabilities[key]) for key in ("base", "bull", "bear")},
                "source": "option_agent",
            }),
            Jsonb({
                "selector": "deterministic_option_pipeline",
                "candidate_decision_id": decision_id,
                "agent_may_clear_execution_gates": False,
            }),
            str(result["bull_target_date"]),
            Jsonb(invalidation_rules),
        ],
    ).fetchone()
    return {
        "status": "materialized",
        "thesis_revision_id": int(inserted["id"]),
        "expression_id": int(expression["id"]),
        "revision": revision,
    }


def accept_agent_task_result(
    connection: Any,
    *,
    task_id: str,
    task_kind: str,
    result: dict[str, Any],
) -> Any:
    validation: dict[str, Any] = {
        "status": "accepted",
        "authority": "hypothesis_only" if task_kind == "option_thesis" else "proposal_only",
    }
    if task_kind == "option_thesis":
        validation["materialization"] = materialize_option_thesis(
            connection,
            task_id=task_id,
            result=result,
        )
    return connection.execute(
        """
        UPDATE analysis.agent_task
        SET status = 'completed', result = %s, validation = %s, updated_at = now()
        WHERE id = %s AND task_kind = %s
        RETURNING id
        """,
        [Jsonb(jsonable(result)), Jsonb(validation), task_id, task_kind],
    ).fetchone()


def option_thesis_materialization_summary(connection: Any) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT
            count(*) FILTER (WHERE status = 'completed') AS completed,
            count(*) FILTER (
                WHERE validation->'materialization'->>'status' IN ('materialized', 'already_materialized')
            ) AS materialized,
            count(*) FILTER (
                WHERE status = 'completed'
                  AND validation->'materialization'->>'status' = 'blocked_manual_lock'
            ) AS blocked_manual_lock,
            count(*) FILTER (
                WHERE task.status = 'completed'
                  AND task.validation->'materialization' IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM catalog.instrument instrument
                      JOIN app.thesis thesis ON thesis.instrument_id = instrument.id
                      JOIN app.thesis_expression expression ON expression.thesis_revision_id = thesis.id
                      WHERE instrument.symbol = task.request->>'ticker'
                        AND thesis.status = 'current' AND expression.status = 'active'
                  )
            ) AS historical_unmaterialized
            , count(*) FILTER (
                WHERE task.status = 'completed'
                  AND task.validation->'materialization' IS NULL
                  AND EXISTS (
                      SELECT 1
                      FROM catalog.instrument instrument
                      JOIN app.thesis thesis ON thesis.instrument_id = instrument.id
                      JOIN app.thesis_expression expression ON expression.thesis_revision_id = thesis.id
                      WHERE instrument.symbol = task.request->>'ticker'
                        AND thesis.status = 'current' AND expression.status = 'active'
                  )
            ) AS historical_superseded
        FROM analysis.agent_task task
        WHERE task.task_kind = 'option_thesis'
        """
    ).fetchone()
    return {
        "completed": int(row["completed"]),
        "materialized": int(row["materialized"]),
        "blocked_manual_lock": int(row["blocked_manual_lock"]),
        "historical_unmaterialized": int(row["historical_unmaterialized"]),
        "historical_superseded": int(row["historical_superseded"]),
        "authority": "research_only",
        "recommendation_owner": "canonical_option_trade_ticket",
    }


def _evidence_reference(item: Any) -> str:
    if isinstance(item, dict):
        source_type = str(item.get("type") or "evidence").strip()
        source_id = str(item.get("id") or "").strip()
        return f"{source_type}:{source_id}" if source_id else ""
    return str(item or "").strip()
