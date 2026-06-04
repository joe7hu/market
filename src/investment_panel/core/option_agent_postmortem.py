"""Structured agent postmortem handoff for the options radar learning loop."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from investment_panel.core.db import json_dumps, query_rows
from investment_panel.core.source_ingestion.utils import stable_id


AGENT_POSTMORTEM_VERSION = "option-postmortem-agent-v1"
POSTMORTEM_SOURCE_LIMIT = 20


class AgentPostmortemValidationError(ValueError):
    """Raised when an agent postmortem does not satisfy the structured contract."""


def refresh_option_agent_postmortem_work(con: Any, *, strategy_version: str, limit: int = POSTMORTEM_SOURCE_LIMIT) -> dict[str, int]:
    requests = refresh_agent_postmortem_requests(con, strategy_version=strategy_version, limit=limit)
    proposals = materialize_agent_postmortem_strategy_proposals(con, strategy_version=strategy_version)
    return {
        "agent_postmortem_requests": requests,
        "agent_postmortem_strategy_proposals": proposals,
    }


def refresh_agent_postmortem_requests(con: Any, *, strategy_version: str, limit: int = POSTMORTEM_SOURCE_LIMIT) -> int:
    sources = important_outcome_sources(con, strategy_version=strategy_version, limit=limit)
    count = 0
    for source in sources:
        request = build_agent_postmortem_request(con, source)
        before = query_rows(con, "SELECT count(*) AS count FROM agent_postmortem_request WHERE request_id = ?", [request["request_id"]])[0]["count"]
        con.execute(
            """
            INSERT OR IGNORE INTO agent_postmortem_request
            (request_id, created_at, source_type, source_id, ticker,
             strategy_version, priority_score, status, prompt, context, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                request["request_id"],
                request["created_at"],
                request["source_type"],
                request["source_id"],
                request["ticker"],
                request["strategy_version"],
                request["priority_score"],
                request["status"],
                request["prompt"],
                json_dumps(request["context"]),
                json_dumps(request["raw"]),
            ],
        )
        after = query_rows(con, "SELECT count(*) AS count FROM agent_postmortem_request WHERE request_id = ?", [request["request_id"]])[0]["count"]
        count += int(after) - int(before)
    return count


def important_outcome_sources(con: Any, *, strategy_version: str, limit: int) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    sources.extend(_missed_winner_sources(con, strategy_version=strategy_version, limit=limit))
    sources.extend(_shadow_winner_sources(con, strategy_version=strategy_version, limit=limit))
    sources.extend(_shadow_loser_sources(con, strategy_version=strategy_version, limit=limit))
    sources.extend(_thesis_invalidated_sources(con, strategy_version=strategy_version, limit=limit))
    sources.sort(key=lambda item: float(item.get("priority_score") or 0), reverse=True)
    return _dedupe_sources(sources)[:limit]


def build_agent_postmortem_request(con: Any, source: dict[str, Any]) -> dict[str, Any]:
    source_type = str(source["source_type"])
    source_id = str(source["source_id"])
    ticker = str(source.get("ticker") or "").upper()
    strategy_version = str(source.get("strategy_version") or "")
    context = {
        "source": decode_json_fields(source, ("raw",)),
        "candidate_event": _candidate_context(con, source),
        "latest_attribution": _latest_attribution_context(con, source),
        "latest_thesis_validation": _latest_thesis_validation_context(con, ticker),
    }
    return {
        "request_id": stable_id("agent_postmortem_request", strategy_version, source_type, source_id),
        "created_at": datetime.utcnow().isoformat(),
        "source_type": source_type,
        "source_id": source_id,
        "ticker": ticker,
        "strategy_version": strategy_version,
        "priority_score": source.get("priority_score"),
        "status": "open",
        "prompt": agent_postmortem_prompt(ticker, source_type),
        "context": context,
        "raw": {"authority": "hypothesis_only", "required_output": "agent_postmortem"},
    }


def agent_postmortem_prompt(ticker: str, source_type: str) -> str:
    return (
        f"Write a structured options-radar postmortem for {ticker or 'this ticker'} based on {source_type}. "
        "Return JSON only with keys: ticker, outcome_type, failure_type, evidence, "
        "proposed_rule_change, proposed_parameter_changes, expected_effect, risk, "
        "confidence, evidence_refs. Agents may propose strategy mutations only; "
        "do not recommend, pick, execute, or promote trades."
    )


def upsert_agent_postmortem(con: Any, payload: dict[str, Any], *, agent_version: str = AGENT_POSTMORTEM_VERSION) -> str:
    postmortem = normalize_agent_postmortem(payload, agent_version=agent_version)
    con.execute(
        """
        INSERT OR REPLACE INTO agent_postmortem
        (postmortem_id, request_id, source_type, source_id, created_at,
         agent_version, ticker, strategy_version, outcome_type, failure_type,
         evidence, proposed_rule_change, proposed_parameter_changes,
         expected_effect, risk, confidence, evidence_refs, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            postmortem["postmortem_id"],
            postmortem["request_id"],
            postmortem["source_type"],
            postmortem["source_id"],
            postmortem["created_at"],
            postmortem["agent_version"],
            postmortem["ticker"],
            postmortem["strategy_version"],
            postmortem["outcome_type"],
            postmortem["failure_type"],
            json_dumps(postmortem["evidence"]),
            postmortem["proposed_rule_change"],
            json_dumps(postmortem["proposed_parameter_changes"]),
            postmortem["expected_effect"],
            postmortem["risk"],
            postmortem["confidence"],
            json_dumps(postmortem["evidence_refs"]),
            json_dumps(postmortem["raw"]),
        ],
    )
    if postmortem.get("request_id"):
        con.execute(
            "UPDATE agent_postmortem_request SET status = 'fulfilled' WHERE request_id = ?",
            [postmortem["request_id"]],
        )
    materialize_agent_postmortem_strategy_proposals(con, strategy_version=postmortem["strategy_version"])
    return postmortem["postmortem_id"]


def normalize_agent_postmortem(payload: dict[str, Any], *, agent_version: str) -> dict[str, Any]:
    request = _request_context(payload)
    ticker = str(payload.get("ticker") or request.get("ticker") or "").upper()
    strategy_version = str(payload.get("strategy_version") or request.get("strategy_version") or "")
    source_type = str(payload.get("source_type") or request.get("source_type") or "")
    source_id = str(payload.get("source_id") or request.get("source_id") or "")
    outcome_type = str(payload.get("outcome_type") or "").strip()
    failure_type = str(payload.get("failure_type") or "").strip()
    evidence = _string_list(payload.get("evidence"))
    proposed_rule_change = str(payload.get("proposed_rule_change") or "").strip()
    proposed_parameter_changes = _dict_value(payload.get("proposed_parameter_changes"))
    expected_effect = str(payload.get("expected_effect") or "").strip()
    risk = str(payload.get("risk") or "").strip()
    evidence_refs = _list_value(payload.get("evidence_refs"))
    confidence = _number(payload.get("confidence"))
    missing = []
    for field, value in (
        ("ticker", ticker),
        ("strategy_version", strategy_version),
        ("source_type", source_type),
        ("source_id", source_id),
        ("outcome_type", outcome_type),
        ("failure_type", failure_type),
        ("evidence", evidence),
        ("proposed_rule_change", proposed_rule_change),
        ("expected_effect", expected_effect),
        ("risk", risk),
    ):
        if not value:
            missing.append(field)
    if missing:
        raise AgentPostmortemValidationError(f"agent postmortem missing required fields: {', '.join(missing)}")
    created_at = str(payload.get("created_at") or datetime.utcnow().isoformat())
    request_id = str(payload.get("request_id") or request.get("request_id") or "")
    postmortem_id = str(payload.get("postmortem_id") or stable_id("agent_postmortem", request_id, source_type, source_id, agent_version, created_at))
    return {
        "postmortem_id": postmortem_id,
        "request_id": request_id or None,
        "source_type": source_type,
        "source_id": source_id,
        "created_at": created_at,
        "agent_version": agent_version,
        "ticker": ticker,
        "strategy_version": strategy_version,
        "outcome_type": outcome_type,
        "failure_type": failure_type,
        "evidence": evidence,
        "proposed_rule_change": proposed_rule_change,
        "proposed_parameter_changes": proposed_parameter_changes,
        "expected_effect": expected_effect,
        "risk": risk,
        "confidence": max(0.0, min(100.0, confidence if confidence is not None else 50.0)),
        "evidence_refs": evidence_refs,
        "raw": {**payload, "authority": "proposal_only", "promotion_policy": "deterministic_gates_required"},
    }


def materialize_agent_postmortem_strategy_proposals(con: Any, *, strategy_version: str) -> int:
    rows = query_rows(
        con,
        """
        SELECT *
        FROM agent_postmortem
        WHERE strategy_version = ?
        ORDER BY created_at DESC
        """,
        [strategy_version],
    )
    count = 0
    for row in rows:
        changes = _dict_value(row.get("proposed_parameter_changes"))
        if not changes:
            continue
        proposal = build_postmortem_strategy_proposal(row, changes)
        before = query_rows(con, "SELECT count(*) AS count FROM strategy_mutation_proposal WHERE proposal_id = ?", [proposal["proposal_id"]])[0]["count"]
        con.execute(
            """
            INSERT OR IGNORE INTO strategy_mutation_proposal
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


def build_postmortem_strategy_proposal(postmortem: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    strategy_version = str(postmortem.get("strategy_version") or "")
    failure_slug = _slug(str(postmortem.get("failure_type") or "agent_change"))
    postmortem_id = str(postmortem.get("postmortem_id") or "")
    source_id = str(postmortem.get("source_id") or "")
    return {
        "proposal_id": stable_id("strategy_mutation_proposal", "agent_postmortem", postmortem_id),
        "created_at": datetime.utcnow().isoformat(),
        "source_type": "agent_postmortem",
        "strategy_version": strategy_version,
        "proposed_strategy_version": f"{strategy_version}_{failure_slug}_agent_proposed_v1",
        "proposed_parameter_changes": changes,
        "rationale": f"{postmortem.get('failure_type')}: {postmortem.get('proposed_rule_change')}",
        "expected_effect": str(postmortem.get("expected_effect") or ""),
        "risk": str(postmortem.get("risk") or ""),
        "status": "proposed",
        "requires_backtest": True,
        "requires_forward_test": True,
        "human_approval_status": "required",
        "evidence_refs": [
            {"type": "agent_postmortem", "id": postmortem_id},
            {"type": str(postmortem.get("source_type") or "source"), "id": source_id},
            *_list_value(postmortem.get("evidence_refs")),
        ],
        "raw": {
            "agent_version": postmortem.get("agent_version"),
            "outcome_type": postmortem.get("outcome_type"),
            "confidence": postmortem.get("confidence"),
            "promotion_policy": "no_auto_promotion",
        },
    }


def _missed_winner_sources(con: Any, *, strategy_version: str, limit: int) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT missed_id AS source_id, 'missed_winner' AS source_type, ticker,
               strategy_version, max_return_seen AS priority_score, raw
        FROM missed_winner_event
        WHERE strategy_version = ?
        ORDER BY max_return_seen DESC, detected_at DESC
        LIMIT ?
        """,
        [strategy_version, limit],
    )
    return rows


def _shadow_winner_sources(con: Any, *, strategy_version: str, limit: int) -> list[dict[str, Any]]:
    return query_rows(
        con,
        """
        SELECT st.trade_id AS source_id,
               CASE WHEN st.time_to_10x IS NOT NULL OR st.max_return_seen >= 9.0
                    THEN 'shadow_big_winner_10x' ELSE 'shadow_big_winner_5x' END AS source_type,
               ce.ticker, ce.strategy_version, st.max_return_seen AS priority_score, st.raw
        FROM shadow_trade st
        JOIN candidate_event ce ON ce.event_id = st.event_id
        WHERE ce.strategy_version = ?
              AND (st.time_to_5x IS NOT NULL OR st.time_to_10x IS NOT NULL OR st.max_return_seen >= 4.0)
        ORDER BY st.max_return_seen DESC, st.entry_time DESC
        LIMIT ?
        """,
        [strategy_version, limit],
    )


def _shadow_loser_sources(con: Any, *, strategy_version: str, limit: int) -> list[dict[str, Any]]:
    return query_rows(
        con,
        """
        SELECT st.trade_id AS source_id, 'shadow_big_loser' AS source_type,
               ce.ticker, ce.strategy_version, abs(st.max_drawdown_seen) AS priority_score, st.raw
        FROM shadow_trade st
        JOIN candidate_event ce ON ce.event_id = st.event_id
        WHERE ce.strategy_version = ?
              AND st.max_drawdown_seen <= -0.40
              AND st.time_to_2x IS NULL
        ORDER BY abs(st.max_drawdown_seen) DESC, st.entry_time DESC
        LIMIT ?
        """,
        [strategy_version, limit],
    )


def _thesis_invalidated_sources(con: Any, *, strategy_version: str, limit: int) -> list[dict[str, Any]]:
    return query_rows(
        con,
        """
        SELECT v.validation_id AS source_id,
               CASE WHEN v.state = 'invalidated' THEN 'thesis_invalidated' ELSE 'thesis_weakening' END AS source_type,
               v.ticker, ? AS strategy_version,
               CASE WHEN v.state = 'invalidated' THEN 3.0 ELSE 2.0 END AS priority_score,
               v.raw
        FROM agent_thesis_validation v
        WHERE v.state IN ('invalidated', 'weakening')
        ORDER BY CASE WHEN v.state = 'invalidated' THEN 0 ELSE 1 END, v.validated_at DESC
        LIMIT ?
        """,
        [strategy_version, limit],
    )


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in sources:
        key = (str(source.get("source_type") or ""), str(source.get("source_id") or ""))
        if key in seen or not key[1]:
            continue
        seen.add(key)
        output.append(source)
    return output


def _candidate_context(con: Any, source: dict[str, Any]) -> dict[str, Any] | None:
    source_type = str(source.get("source_type") or "")
    source_id = str(source.get("source_id") or "")
    if source_type.startswith("shadow"):
        rows = query_rows(
            con,
            """
            SELECT ce.*
            FROM candidate_event ce
            JOIN shadow_trade st ON st.event_id = ce.event_id
            WHERE st.trade_id = ?
            LIMIT 1
            """,
            [source_id],
        )
    elif source_type == "missed_winner":
        rows = query_rows(
            con,
            """
            SELECT ce.*
            FROM candidate_event ce
            JOIN missed_winner_event mw ON mw.contract_id = ce.contract_id
                 AND mw.strategy_version = ce.strategy_version
            WHERE mw.missed_id = ?
            ORDER BY ce.snapshot_time
            LIMIT 1
            """,
            [source_id],
        )
    else:
        rows = []
    return decode_json_fields(rows[0], ("raw",)) if rows else None


def _latest_attribution_context(con: Any, source: dict[str, Any]) -> dict[str, Any] | None:
    source_type = str(source.get("source_type") or "")
    source_id = str(source.get("source_id") or "")
    if not source_type.startswith("shadow"):
        return None
    rows = query_rows(
        con,
        """
        SELECT a.*
        FROM option_attribution a
        JOIN shadow_trade st ON st.event_id = a.event_id
        WHERE st.trade_id = ?
        ORDER BY a.snapshot_time DESC
        LIMIT 1
        """,
        [source_id],
    )
    return decode_json_fields(rows[0], ("raw",)) if rows else None


def _latest_thesis_validation_context(con: Any, ticker: str) -> dict[str, Any] | None:
    if not ticker:
        return None
    rows = query_rows(
        con,
        """
        SELECT *
        FROM agent_thesis_validation
        WHERE ticker = ?
        ORDER BY validated_at DESC
        LIMIT 1
        """,
        [ticker],
    )
    return decode_json_fields(rows[0], ("evidence_refs", "raw")) if rows else None


def _request_context(payload: dict[str, Any]) -> dict[str, Any]:
    request = payload.get("request")
    return request if isinstance(request, dict) else {}


def decode_json_fields(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    decoded = dict(row)
    for field in fields:
        if field in decoded:
            decoded[field] = _json_or_value(decoded[field])
    return decoded


def _json_or_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    decoded = _json_or_value(value)
    if isinstance(decoded, list):
        return decoded
    return [value] if isinstance(value, str) and value else []


def _dict_value(value: Any) -> dict[str, Any]:
    decoded = _json_or_value(value)
    return decoded if isinstance(decoded, dict) else {}


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "agent_change"
