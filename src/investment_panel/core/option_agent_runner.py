"""Run local external agents for options-radar hypothesis handoffs."""

from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any

from investment_panel.core.db import json_dumps, query_rows
from investment_panel.core.option_agent_postmortem import materialize_agent_postmortem_strategy_proposals, upsert_agent_postmortem
from investment_panel.core.option_agent_thesis import (
    attach_agent_theses_to_candidates,
    decode_json_fields,
    refresh_agent_thesis_validations,
    upsert_agent_thesis,
)
from investment_panel.core.options_radar import (
    DEFAULT_STRATEGY_VERSION,
    apply_shadow_trade_exits,
    create_shadow_trades,
    refresh_option_radar_opportunities,
    refresh_radar_state_transitions,
    refresh_strategy_proposal_evaluations,
)


AGENT_THESIS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "ticker",
        "bull_target_price",
        "bull_target_date",
        "base_target_price",
        "core_thesis",
        "required_proofs",
        "catalysts",
        "invalidation",
        "bear_case",
        "confidence",
        "evidence_refs",
    ],
    "properties": {
        "ticker": "Uppercase ticker symbol.",
        "bull_target_price": "Numeric upside target required by the option math.",
        "bull_target_date": "ISO date by which the bull target should be plausible.",
        "base_target_price": "Numeric base-case target used for thesis progress validation.",
        "core_thesis": "Short falsifiable thesis.",
        "required_proofs": "Array of observable proof points.",
        "catalysts": "Array of catalysts with type/expected_window/what_to_watch.",
        "invalidation": "Array of concrete invalidation conditions.",
        "bear_case": "Best opposing case.",
        "confidence": "0-100 hypothesis confidence, not a trade recommendation.",
        "evidence_refs": "Array of stored evidence references.",
    },
}

AGENT_POSTMORTEM_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "ticker",
        "strategy_version",
        "source_type",
        "source_id",
        "outcome_type",
        "failure_type",
        "evidence",
        "proposed_rule_change",
        "proposed_parameter_changes",
        "expected_effect",
        "risk",
        "confidence",
        "evidence_refs",
    ],
    "properties": {
        "outcome_type": "winner, loser, missed_winner, false_alert, thesis_invalidated, or similar.",
        "failure_type": "Machine-readable reason such as timing_too_early or delta_too_low.",
        "evidence": "Array of factual observations from the provided context.",
        "proposed_rule_change": "Human-readable strategy mutation proposal.",
        "proposed_parameter_changes": "Object of candidate parameter changes only.",
        "expected_effect": "Expected deterministic metric improvement.",
        "risk": "How the change could hurt the current strategy.",
        "confidence": "0-100 confidence in the diagnosis.",
        "evidence_refs": "Array of stored evidence references.",
    },
}


class ExternalOptionAgentError(RuntimeError):
    """Raised when an external option agent cannot return structured JSON."""


def run_external_option_agents(
    con: Any,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    thesis_command: str = "",
    thesis_limit: int = 20,
    thesis_timeout_seconds: int = 120,
    postmortem_command: str = "",
    postmortem_limit: int = 20,
    postmortem_timeout_seconds: int = 120,
) -> dict[str, Any]:
    thesis = run_external_agent_thesis_requests(
        con,
        strategy_version=strategy_version,
        command=thesis_command,
        limit=thesis_limit,
        timeout_seconds=thesis_timeout_seconds,
    )
    postmortem = run_external_agent_postmortem_requests(
        con,
        strategy_version=strategy_version,
        command=postmortem_command,
        limit=postmortem_limit,
        timeout_seconds=postmortem_timeout_seconds,
    )
    return {
        "strategy_version": strategy_version,
        "agent_thesis_runner": thesis,
        "agent_postmortem_runner": postmortem,
    }


_BATCH_GUARDRAILS: dict[str, Any] = {
    "authority": "hypothesis_only",
    "deterministic_code_owns": ["facts", "math", "storage", "validation", "scoring", "backtests", "promotion"],
    "agent_owns": ["interpretation", "thesis_generation", "red_team", "catalyst_extraction", "proposal_drafting"],
    "forbidden": ["trade_execution", "silent_strategy_promotion", "unstructured_prose_response"],
}


def run_consolidated_option_agents(
    con: Any,
    *,
    command: str,
    limit_thesis: int = 8,
    limit_postmortem: int = 4,
    timeout_seconds: int = 180,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
) -> dict[str, Any]:
    """Single batched pass: one agent invocation covers thesis + postmortem.

    Gathers all open requests, builds ONE payload (shared guardrails + both
    schemas), invokes the command exactly once, and dispatches the structured
    outputs back through the existing upsert paths. No-op when there are no open
    requests, so the cost is paid only when there is work.
    """

    if not command:
        return {"enabled": False, "skipped_reason": "no_option_agent_command", "attempted": 0, "accepted": 0, "failed": 0}

    thesis_rows = _open_request_rows(con, "agent_thesis_request", strategy_version=strategy_version, limit=limit_thesis)
    postmortem_rows = _open_request_rows(con, "agent_postmortem_request", strategy_version=strategy_version, limit=limit_postmortem)
    if not thesis_rows and not postmortem_rows:
        return {"enabled": True, "skipped_reason": "no_open_requests", "attempted": 0, "accepted": 0, "failed": 0}

    payload = {
        "thesis": [_agent_request_payload(row, output_schema=AGENT_THESIS_OUTPUT_SCHEMA) for row in thesis_rows],
        "postmortem": [_agent_request_payload(row, output_schema=AGENT_POSTMORTEM_OUTPUT_SCHEMA) for row in postmortem_rows],
        "guardrails": _BATCH_GUARDRAILS,
        "output_schemas": {"thesis": AGENT_THESIS_OUTPUT_SCHEMA, "postmortem": AGENT_POSTMORTEM_OUTPUT_SCHEMA},
    }

    attempted = len(thesis_rows) + len(postmortem_rows)
    try:
        output = _invoke_agent_command(command, payload, timeout_seconds=timeout_seconds)
    except Exception as exc:
        for table, row in [("agent_thesis_request", r) for r in thesis_rows] + [("agent_postmortem_request", r) for r in postmortem_rows]:
            _mark_request_failed(con, table, str(row["request_id"]), exc)
        return {"enabled": True, "attempted": attempted, "accepted": 0, "failed": attempted, "error": str(exc)}

    thesis_out = output.get("thesis") if isinstance(output.get("thesis"), list) else []
    postmortem_out = output.get("postmortem") if isinstance(output.get("postmortem"), list) else []

    thesis_accepted, thesis_failures = _dispatch_batch_outputs(
        con, thesis_rows, thesis_out, table_name="agent_thesis_request", upsert=upsert_agent_thesis
    )
    postmortem_accepted, postmortem_failures = _dispatch_batch_outputs(
        con, postmortem_rows, postmortem_out, table_name="agent_postmortem_request", upsert=upsert_agent_postmortem
    )

    followup: dict[str, Any] = {}
    if thesis_accepted:
        followup["thesis_followup"] = _refresh_after_agent_theses(con, strategy_version=strategy_version)
    if postmortem_accepted:
        followup["postmortem_followup"] = _refresh_after_agent_postmortems(con, strategy_version=strategy_version)

    return {
        "enabled": True,
        "attempted": attempted,
        "accepted": thesis_accepted + postmortem_accepted,
        "failed": len(thesis_failures) + len(postmortem_failures),
        "thesis": {"attempted": len(thesis_rows), "accepted": thesis_accepted, "failures": thesis_failures},
        "postmortem": {"attempted": len(postmortem_rows), "accepted": postmortem_accepted, "failures": postmortem_failures},
        "strategy_version": strategy_version,
        **followup,
    }


def _dispatch_batch_outputs(
    con: Any,
    rows: list[dict[str, Any]],
    outputs: list[Any],
    *,
    table_name: str,
    upsert: Any,
) -> tuple[int, list[dict[str, Any]]]:
    accepted = 0
    failures: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        output = outputs[index] if index < len(outputs) and isinstance(outputs[index], dict) else None
        if output is None:
            exc = ExternalOptionAgentError("agent returned no output for request")
            _mark_request_failed(con, table_name, str(row["request_id"]), exc)
            failures.append({"request_id": row.get("request_id"), "ticker": row.get("ticker"), "error": str(exc)})
            continue
        try:
            upsert(con, _with_request_defaults(output, row))
            accepted += 1
        except Exception as exc:
            _mark_request_failed(con, table_name, str(row["request_id"]), exc)
            failures.append({"request_id": row.get("request_id"), "ticker": row.get("ticker"), "error": str(exc)})
    return accepted, failures


def run_external_agent_thesis_requests(
    con: Any,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    command: str = "",
    limit: int = 20,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    if not command:
        return {"enabled": False, "skipped_reason": "no_option_thesis_command", "attempted": 0, "accepted": 0, "failed": 0}

    rows = _open_request_rows(con, "agent_thesis_request", strategy_version=strategy_version, limit=limit)
    accepted = 0
    failures: list[dict[str, Any]] = []
    for row in rows:
        payload = _agent_request_payload(row, output_schema=AGENT_THESIS_OUTPUT_SCHEMA)
        try:
            output = _invoke_agent_command(command, payload, timeout_seconds=timeout_seconds)
            output = _with_request_defaults(output, row)
            upsert_agent_thesis(con, output)
            accepted += 1
        except Exception as exc:
            _mark_request_failed(con, "agent_thesis_request", str(row["request_id"]), exc)
            failures.append({"request_id": row.get("request_id"), "ticker": row.get("ticker"), "error": str(exc)})

    followup = _refresh_after_agent_theses(con, strategy_version=strategy_version) if accepted else {}
    return {
        "enabled": True,
        "attempted": len(rows),
        "accepted": accepted,
        "failed": len(failures),
        "failures": failures,
        **followup,
    }


def run_external_agent_postmortem_requests(
    con: Any,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    command: str = "",
    limit: int = 20,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    if not command:
        return {"enabled": False, "skipped_reason": "no_option_postmortem_command", "attempted": 0, "accepted": 0, "failed": 0}

    rows = _open_request_rows(con, "agent_postmortem_request", strategy_version=strategy_version, limit=limit)
    accepted = 0
    failures: list[dict[str, Any]] = []
    for row in rows:
        payload = _agent_request_payload(row, output_schema=AGENT_POSTMORTEM_OUTPUT_SCHEMA)
        try:
            output = _invoke_agent_command(command, payload, timeout_seconds=timeout_seconds)
            output = _with_request_defaults(output, row)
            upsert_agent_postmortem(con, output)
            accepted += 1
        except Exception as exc:
            _mark_request_failed(con, "agent_postmortem_request", str(row["request_id"]), exc)
            failures.append({"request_id": row.get("request_id"), "ticker": row.get("ticker"), "error": str(exc)})

    followup = _refresh_after_agent_postmortems(con, strategy_version=strategy_version) if accepted else {}
    return {
        "enabled": True,
        "attempted": len(rows),
        "accepted": accepted,
        "failed": len(failures),
        "failures": failures,
        **followup,
    }


def _open_request_rows(con: Any, table_name: str, *, strategy_version: str, limit: int) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        f"""
        SELECT *
        FROM {table_name}
        WHERE strategy_version = ? AND status = 'open'
        ORDER BY priority_score DESC NULLS LAST, created_at DESC
        LIMIT ?
        """,
        [strategy_version, limit],
    )
    return [decode_json_fields(row, ("context", "raw")) for row in rows]


def _agent_request_payload(row: dict[str, Any], *, output_schema: dict[str, Any]) -> dict[str, Any]:
    request = {
        "request_id": row.get("request_id"),
        "ticker": row.get("ticker"),
        "event_id": row.get("event_id"),
        "source_type": row.get("source_type"),
        "source_id": row.get("source_id"),
        "strategy_version": row.get("strategy_version"),
        "priority_score": row.get("priority_score"),
        "created_at": _string_or_none(row.get("created_at")),
    }
    return {
        "request": {key: value for key, value in request.items() if value not in (None, "")},
        "prompt": row.get("prompt"),
        "context": row.get("context") or {},
        "output_schema": output_schema,
        "guardrails": {
            "authority": "hypothesis_only",
            "deterministic_code_owns": ["facts", "math", "storage", "validation", "scoring", "backtests", "promotion"],
            "agent_owns": ["interpretation", "thesis_generation", "red_team", "catalyst_extraction", "proposal_drafting"],
            "forbidden": ["trade_execution", "silent_strategy_promotion", "unstructured_prose_response"],
        },
    }


def _invoke_agent_command(command: str, payload: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            shlex.split(command),
            input=json.dumps(payload, default=str),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalOptionAgentError(f"agent command timed out after {timeout_seconds}s") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        raise ExternalOptionAgentError(f"agent command exited {completed.returncode}: {stderr[:500]}")
    stdout = completed.stdout.strip()
    if not stdout:
        raise ExternalOptionAgentError("agent command returned empty stdout")
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ExternalOptionAgentError(f"agent command returned invalid JSON: {stdout[:500]}") from exc
    if not isinstance(parsed, dict):
        raise ExternalOptionAgentError("agent command must return a JSON object")
    return parsed


def _with_request_defaults(output: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    request = {
        "request_id": row.get("request_id"),
        "ticker": row.get("ticker"),
        "event_id": row.get("event_id"),
        "source_type": row.get("source_type"),
        "source_id": row.get("source_id"),
        "strategy_version": row.get("strategy_version"),
    }
    payload = {**output}
    payload.setdefault("ticker", row.get("ticker"))
    payload.setdefault("strategy_version", row.get("strategy_version"))
    payload.setdefault("request", {key: value for key, value in request.items() if value not in (None, "")})
    refs = payload.get("evidence_refs")
    if not isinstance(refs, list):
        refs = []
    refs = [
        {"type": "agent_request", "id": row.get("request_id")},
        *refs,
    ]
    payload["evidence_refs"] = refs
    return payload


def _mark_request_failed(con: Any, table_name: str, request_id: str, exc: Exception) -> None:
    rows = query_rows(con, f"SELECT raw FROM {table_name} WHERE request_id = ?", [request_id])
    raw = {}
    if rows:
        decoded = decode_json_fields(rows[0], ("raw",))
        raw = decoded.get("raw") if isinstance(decoded.get("raw"), dict) else {}
    con.execute(
        f"""
        UPDATE {table_name}
        SET status = 'agent_failed', raw = ?
        WHERE request_id = ?
        """,
        [json_dumps({**raw, "last_agent_error": str(exc)}), request_id],
    )


def _refresh_after_agent_theses(con: Any, *, strategy_version: str) -> dict[str, Any]:
    attached_rows = attach_agent_theses_to_candidates(con, strategy_version=strategy_version)
    validation_rows = refresh_agent_thesis_validations(con, strategy_version=strategy_version)
    shadow_trades = create_shadow_trades(con, strategy_version=strategy_version)
    transitions = refresh_radar_state_transitions(con, strategy_version=strategy_version)
    opportunities = refresh_option_radar_opportunities(con, strategy_version=strategy_version)
    exits = apply_shadow_trade_exits(con, strategy_version=strategy_version)
    return {
        "agent_work": {
            "agent_thesis_requests": 0,
            "agent_thesis_requests_superseded": 0,
            "agent_theses_attached": attached_rows,
            "agent_thesis_validations": validation_rows,
        },
        "shadow_trades": shadow_trades,
        "radar_state_transitions": transitions,
        "option_radar_opportunities": opportunities,
        "shadow_trades_exited": exits,
    }


def _refresh_after_agent_postmortems(con: Any, *, strategy_version: str) -> dict[str, Any]:
    proposals = materialize_agent_postmortem_strategy_proposals(con, strategy_version=strategy_version)
    evaluations = refresh_strategy_proposal_evaluations(con, strategy_version=strategy_version)
    return {
        "postmortem_work": {
            "agent_postmortem_requests": 0,
            "agent_postmortem_strategy_proposals": proposals,
        },
        **evaluations,
    }


def _string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
