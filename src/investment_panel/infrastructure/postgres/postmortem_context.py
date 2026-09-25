"""Bounded, identity-bound evidence for advisory postmortems, not trade authority."""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from typing import Any

from investment_panel.infrastructure.postgres.agent_process import jsonable
from investment_panel.infrastructure.postgres.options_paper_ledger import paper_fill_totals, reconciled_exit_pnl

CONTEXT_VERSION = "option-postmortem-evidence-v2"


def evidence_context(connection: Any, decision_id: Any, cutoff: datetime) -> dict[str, Any]:
    row = connection.execute("""
        SELECT decision.id, decision.decision_key, decision.state, decision.score,
               decision.reasons, decision.blockers, decision.as_of, decision.input_hash,
               decision.strategy_revision_id, decision.lane, decision.episode_key,
               instrument.symbol AS ticker,
               to_jsonb(outcome) - 'updated_at' AS outcome,
               jsonb_build_object('contract_id', candidate.contract_id, 'snapshot_id', candidate.snapshot_id,
                 'quote_observed_at', candidate.quote_observed_at, 'structure', candidate.structure,
                 'premium_mid', candidate.premium_mid, 'entry_price', candidate.entry_price,
                 'synthetic_legs', candidate.synthetic_legs, 'max_profit', candidate.max_profit,
                 'max_loss', candidate.max_loss, 'break_even', candidate.break_even,
                 'thesis_id', candidate.thesis_id, 'model_version', candidate.model_version) AS candidate,
               jsonb_build_object('id', contract.id, 'expiration', contract.expiration,
                 'strike', contract.strike, 'option_type', contract.option_type,
                 'multiplier', contract.multiplier, 'deliverable_key', contract.deliverable_key,
                 'style', contract.style, 'settlement', contract.settlement) AS contract,
               jsonb_build_object('id', strategy.id, 'strategy_key', strategy.strategy_key,
                 'revision', strategy.revision, 'parameters', strategy.parameters) AS strategy,
               thesis.thesis AS entry_thesis
        FROM analysis.decision decision
        JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
        LEFT JOIN analysis.option_outcome outcome ON outcome.decision_id = decision.id
          AND outcome.updated_at <= %s
        LEFT JOIN analysis.option_decision candidate ON candidate.decision_id = decision.id
        LEFT JOIN catalog.option_contract contract ON contract.id = candidate.contract_id
        LEFT JOIN analysis.strategy_revision strategy ON strategy.id = decision.strategy_revision_id
        LEFT JOIN app.thesis thesis ON thesis.id = candidate.thesis_id
          AND thesis.created_at <= decision.as_of
          AND (thesis.updated_at <= decision.as_of OR thesis.status = 'superseded')
        WHERE decision.id = %s AND decision.as_of <= %s
        """, [cutoff, decision_id, cutoff]).fetchone()
    if row is None:
        raise ValueError(f"decision not available at postmortem cutoff: {decision_id}")
    values = dict(row)
    context = {key: values.pop(key) for key in ("outcome", "candidate", "contract", "strategy", "entry_thesis")}
    context.update(context_version=CONTEXT_VERSION, decision=values, review_cutoff=cutoff,
                   paper_executions=[], evidence_gaps=[], evidence_refs=[{"type": "decision", "id": str(decision_id)}])
    outcome = context["outcome"] or {}
    context["attribution"] = {key: outcome.get(key) for key in (
        "stock_move_effect", "iv_effect", "theta_effect", "spread_effect", "unexplained_effect",
    )}
    if not outcome:
        context["evidence_gaps"].append("outcome_missing_at_cutoff")
    if all(value is None for value in context["attribution"].values()):
        context["evidence_gaps"].append("factor_attribution_unavailable")
    orders = connection.execute("""
        SELECT id, decision_id, status, side, structure, expression_kind, quantity,
               filled_quantity, exited_quantity, filled_at, actual_fill_price, exit_at,
               exit_price, contract_multiplier, entry_fees, fees, thesis_snapshot,
               execution_quote->'assignment' AS settlement,
               jsonb_build_object('structure', ticket_snapshot->'structure') AS ticket_snapshot,
               unfilled_reason
        FROM app.paper_order WHERE decision_id = %s AND paper_only IS TRUE
          AND created_at <= %s AND updated_at <= %s
        ORDER BY created_at, id LIMIT 21
        """, [decision_id, cutoff, cutoff]).fetchall()
    if len(orders) > 20:
        context["evidence_gaps"].append("paper_execution_context_truncated")
    for raw in orders[:20]:
        order = dict(raw)
        # The ticket can contain a whole analysis. The original small thesis and
        # journal cash flows are sufficient; keep only structure for reconciliation.
        order["ticket_snapshot"] = {"structure": (order.get("ticket_snapshot") or {}).get("structure")}
        fills = dict(paper_fill_totals(connection, order, as_of=cutoff) or {})
        exits = connection.execute("""
            SELECT id::text AS journal_id, quantity AS journal_quantity,
                   price AS journal_price, details AS journal_details
            FROM app.trade_journal WHERE details->>'paper_order_id' = %s
              AND decision_id = %s AND instrument_id = (SELECT instrument_id FROM app.paper_order WHERE id = %s)
              AND rationale = 'deterministic_options_paper_execution'
              AND (action = 'paper_exit' OR action LIKE 'paper_exit:%%')
              AND created_at <= %s ORDER BY created_at, id LIMIT 201
            """, [str(order["id"]), decision_id, order["id"], cutoff]).fetchall()
        payoffs = [reconciled_exit_pnl({**order, **dict(exit_row)}, fills) for exit_row in exits[:200]]
        complete = bool(payoffs) and len(exits) <= 200 and all(value is not None for value in payoffs) \
            and fills.get("entry_quantity", 0) > 0 and fills.get("entry_quantity") == fills.get("exit_quantity")
        order.update(fill_evidence=fills, realized_payoff_status="reconciled" if complete else "unreconciled",
                     realized_net_pnl=sum(payoffs) if complete else None)
        if not complete:
            context["evidence_gaps"].append("realized_payoff_unreconciled")
        context["paper_executions"].append(order)
        context["evidence_refs"].append({"type": "paper_order", "id": str(order["id"])})
        context["evidence_refs"].extend({"type": "trade_journal", "id": identity} for identity in (fills.get("journal_ids") or [])[:200])
    if not orders:
        context["evidence_gaps"].append("no_recorded_paper_execution")
    if not context["entry_thesis"] and not any(order.get("thesis_snapshot") for order in orders):
        context["evidence_gaps"].append("entry_thesis_unavailable")
    context["evidence_gaps"] = sorted(set(context["evidence_gaps"]))
    return jsonable(context)


def evidence_fingerprint(context: dict[str, Any]) -> str:
    # Producer heartbeats / review timing alone are not a new independent case.
    def stable(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items()
                    if key not in {"review_cutoff", "updated_at", "refreshed_at", "checked_at"}}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value
    return sha256(json.dumps(stable(context), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
