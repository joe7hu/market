"""Structured agent thesis handoff for the 10x options radar."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

from investment_panel.core.db import json_dumps, query_rows
from investment_panel.core.source_ingestion.utils import stable_id


AGENT_THESIS_VERSION = "option-thesis-agent-v1"
PRICE_RE = re.compile(r"(?:below|under|breaks below|stop(?: at)?|invalidation(?: at)?|\$)\s*\$?([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)


class AgentThesisValidationError(ValueError):
    """Raised when an agent thesis does not satisfy the structured contract."""


def refresh_option_agent_work(con: Any, *, strategy_version: str, limit: int = 20) -> dict[str, int]:
    request_rows = refresh_agent_thesis_requests(con, strategy_version=strategy_version, limit=limit)
    attached_rows = attach_agent_theses_to_candidates(con, strategy_version=strategy_version)
    validation_rows = refresh_agent_thesis_validations(con, strategy_version=strategy_version)
    return {
        "agent_thesis_requests": request_rows,
        "agent_theses_attached": attached_rows,
        "agent_thesis_validations": validation_rows,
    }


def refresh_agent_thesis_requests(con: Any, *, strategy_version: str, limit: int = 20) -> int:
    rows = query_rows(
        con,
        """
        SELECT ce.*
        FROM candidate_event ce
        LEFT JOIN (
            SELECT ticker, thesis_id
            FROM agent_thesis
            QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY created_at DESC) = 1
        ) t ON t.ticker = ce.ticker
        WHERE ce.strategy_version = ?
              AND ce.state IN ('FIRE', 'SETUP', 'WATCH')
              AND t.thesis_id IS NULL
        QUALIFY row_number() OVER (PARTITION BY ce.ticker ORDER BY ce.snapshot_time DESC, ce.score DESC) = 1
        ORDER BY CASE ce.state WHEN 'FIRE' THEN 0 WHEN 'SETUP' THEN 1 ELSE 2 END, ce.score DESC
        LIMIT ?
        """,
        [strategy_version, limit],
    )
    count = 0
    for row in rows:
        request = build_agent_thesis_request(con, row)
        con.execute(
            """
            INSERT OR REPLACE INTO agent_thesis_request
            (request_id, created_at, ticker, event_id, strategy_version,
             priority_score, status, prompt, context, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                request["request_id"],
                request["created_at"],
                request["ticker"],
                request["event_id"],
                request["strategy_version"],
                request["priority_score"],
                request["status"],
                request["prompt"],
                json_dumps(request["context"]),
                json_dumps(request["raw"]),
            ],
        )
        count += 1
    return count


def build_agent_thesis_request(con: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    ticker = str(candidate.get("ticker") or "").upper()
    context = {
        "candidate_event": decode_json_fields(candidate, ("raw",)),
        "stock_features": first_row(
            con,
            "SELECT * FROM stock_features WHERE ticker = ? ORDER BY snapshot_time DESC LIMIT 1",
            [ticker],
            ("raw",),
        ),
        "option_features": first_row(
            con,
            "SELECT * FROM option_features WHERE contract_id = ? ORDER BY snapshot_time DESC LIMIT 1",
            [candidate.get("contract_id")],
            ("raw",),
        ),
        "source_signals": query_decoded(
            con,
            """
            SELECT source_item_id, source_id, symbol, observed_at, signal_type,
                   sentiment, direction, confidence, thesis, antithesis,
                   catalysts, risks, invalidation, evidence_refs
            FROM ticker_source_signals
            WHERE symbol = ?
            ORDER BY observed_at DESC, confidence DESC NULLS LAST
            LIMIT 8
            """,
            [ticker],
            ("catalysts", "risks", "evidence_refs"),
        ),
        "news": query_decoded(
            con,
            """
            SELECT id, published_at, provider, title, related_symbols, link, source
            FROM news_items
            WHERE contains(CAST(related_symbols AS VARCHAR), ?)
            ORDER BY published_at DESC
            LIMIT 6
            """,
            [ticker],
            ("related_symbols",),
        ),
    }
    prompt = agent_thesis_prompt(ticker)
    return {
        "request_id": stable_id("agent_thesis_request", candidate.get("strategy_version"), candidate.get("event_id")),
        "created_at": datetime.utcnow().isoformat(),
        "ticker": ticker,
        "event_id": candidate.get("event_id"),
        "strategy_version": candidate.get("strategy_version"),
        "priority_score": candidate.get("score"),
        "status": "open",
        "prompt": prompt,
        "context": context,
        "raw": {"authority": "hypothesis_only", "required_output": "agent_thesis"},
    }


def agent_thesis_prompt(ticker: str) -> str:
    return (
        f"Generate a structured options thesis for {ticker}. "
        "Return JSON only with keys: ticker, bull_target_price, bull_target_date, "
        "base_target_price, core_thesis, required_proofs, catalysts, invalidation, "
        "bear_case, confidence, evidence_refs. Do not recommend or execute trades."
    )


def upsert_agent_thesis(con: Any, payload: dict[str, Any], *, agent_version: str = AGENT_THESIS_VERSION) -> str:
    thesis = normalize_agent_thesis(payload, agent_version=agent_version)
    con.execute(
        """
        INSERT OR REPLACE INTO agent_thesis
        (thesis_id, ticker, created_at, agent_version, bull_target_price,
         bull_target_date, base_target_price, core_thesis, required_proofs,
         invalidation_conditions, catalysts, catalyst_summary, bear_case,
         confidence, evidence_refs, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            thesis["thesis_id"],
            thesis["ticker"],
            thesis["created_at"],
            thesis["agent_version"],
            thesis["bull_target_price"],
            thesis["bull_target_date"],
            thesis["base_target_price"],
            thesis["core_thesis"],
            json_dumps(thesis["required_proofs"]),
            json_dumps(thesis["invalidation_conditions"]),
            json_dumps(thesis["catalysts"]),
            thesis["catalyst_summary"],
            thesis["bear_case"],
            thesis["confidence"],
            json_dumps(thesis["evidence_refs"]),
            json_dumps(thesis["raw"]),
        ],
    )
    return thesis["thesis_id"]


def normalize_agent_thesis(payload: dict[str, Any], *, agent_version: str) -> dict[str, Any]:
    ticker = str(payload.get("ticker") or "").upper()
    core_thesis = str(payload.get("core_thesis") or "").strip()
    bear_case = str(payload.get("bear_case") or "").strip()
    required_proofs = _string_list(payload.get("required_proofs"))
    invalidation = _string_list(payload.get("invalidation_conditions") or payload.get("invalidation"))
    catalysts = _catalyst_list(payload.get("catalysts"))
    evidence_refs = _list_value(payload.get("evidence_refs"))
    bull_target = _number(payload.get("bull_target_price"))
    base_target = _number(payload.get("base_target_price"))
    bull_target_date = _date_string(payload.get("bull_target_date"))
    confidence = _number(payload.get("confidence"))
    missing = []
    if not ticker:
        missing.append("ticker")
    if bull_target is None:
        missing.append("bull_target_price")
    if not bull_target_date:
        missing.append("bull_target_date")
    if base_target is None:
        missing.append("base_target_price")
    if not core_thesis:
        missing.append("core_thesis")
    if not required_proofs:
        missing.append("required_proofs")
    if not catalysts:
        missing.append("catalysts")
    if not invalidation:
        missing.append("invalidation")
    if not bear_case:
        missing.append("bear_case")
    if missing:
        raise AgentThesisValidationError(f"agent thesis missing required fields: {', '.join(missing)}")
    created_at = str(payload.get("created_at") or datetime.utcnow().isoformat())
    thesis_id = str(payload.get("thesis_id") or stable_id("agent_thesis", ticker, agent_version, created_at, core_thesis))
    return {
        "thesis_id": thesis_id,
        "ticker": ticker,
        "created_at": created_at,
        "agent_version": agent_version,
        "bull_target_price": bull_target,
        "bull_target_date": bull_target_date,
        "base_target_price": base_target,
        "core_thesis": core_thesis,
        "required_proofs": required_proofs,
        "invalidation_conditions": invalidation,
        "catalysts": catalysts,
        "catalyst_summary": _catalyst_summary(catalysts),
        "bear_case": bear_case,
        "confidence": max(0.0, min(100.0, confidence if confidence is not None else 50.0)),
        "evidence_refs": evidence_refs,
        "raw": {**payload, "authority": "hypothesis_only"},
    }


def attach_agent_theses_to_candidates(con: Any, *, strategy_version: str) -> int:
    rows = query_rows(
        con,
        """
        SELECT ticker, thesis_id
        FROM agent_thesis
        QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY created_at DESC) = 1
        """,
    )
    count = 0
    for row in rows:
        before = query_rows(
            con,
            """
            SELECT count(*) AS count
            FROM candidate_event
            WHERE strategy_version = ? AND ticker = ?
                  AND (thesis_id IS NULL OR thesis_id != ?)
            """,
            [strategy_version, row["ticker"], row["thesis_id"]],
        )[0]["count"]
        con.execute(
            """
            UPDATE candidate_event
            SET thesis_id = ?
            WHERE strategy_version = ? AND ticker = ?
                  AND (thesis_id IS NULL OR thesis_id != ?)
            """,
            [row["thesis_id"], strategy_version, row["ticker"], row["thesis_id"]],
        )
        con.execute(
            """
            UPDATE agent_thesis_request
            SET status = 'fulfilled'
            WHERE strategy_version = ? AND ticker = ? AND status = 'open'
            """,
            [strategy_version, row["ticker"]],
        )
        count += int(before)
    return count


def refresh_agent_thesis_validations(con: Any, *, strategy_version: str) -> int:
    rows = query_rows(
        con,
        """
        SELECT *
        FROM agent_thesis
        QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY created_at DESC) = 1
        """,
    )
    count = 0
    for thesis in rows:
        candidate = first_row(
            con,
            """
            SELECT *
            FROM candidate_event
            WHERE ticker = ? AND strategy_version = ?
            ORDER BY snapshot_time DESC, score DESC
            LIMIT 1
            """,
            [thesis["ticker"], strategy_version],
            ("raw",),
        )
        stock = first_row(
            con,
            "SELECT * FROM stock_features WHERE ticker = ? ORDER BY snapshot_time DESC LIMIT 1",
            [thesis["ticker"]],
            ("raw",),
        )
        validation = build_agent_thesis_validation(thesis, candidate, stock)
        con.execute(
            """
            INSERT OR REPLACE INTO agent_thesis_validation
            (validation_id, thesis_id, ticker, validated_at, state, reason,
             option_still_valid, stock_progress, iv_status, candidate_state,
             evidence_refs, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                validation["validation_id"],
                validation["thesis_id"],
                validation["ticker"],
                validation["validated_at"],
                validation["state"],
                validation["reason"],
                validation["option_still_valid"],
                validation["stock_progress"],
                validation["iv_status"],
                validation["candidate_state"],
                json_dumps(validation["evidence_refs"]),
                json_dumps(validation["raw"]),
            ],
        )
        count += 1
    return count


def build_agent_thesis_validation(thesis: dict[str, Any], candidate: dict[str, Any] | None, stock: dict[str, Any] | None) -> dict[str, Any]:
    ticker = str(thesis.get("ticker") or "").upper()
    raw_candidate = _json(candidate.get("raw")) if candidate else {}
    blockers = [str(item) for item in raw_candidate.get("blockers") or []]
    hard_rejects = [str(item) for item in raw_candidate.get("hard_rejects") or []]
    candidate_state = str((candidate or {}).get("state") or "missing_candidate")
    price = _number((stock or {}).get("price"))
    base_target = _number(thesis.get("base_target_price"))
    invalidation = _string_list(thesis.get("invalidation_conditions"))
    invalidation_price = _invalidation_price(invalidation)
    option_still_valid = candidate_state in {"FIRE", "SETUP", "WATCH"} and not hard_rejects
    iv_status = "overpriced" if any("iv" in item for item in [*blockers, *hard_rejects]) else "acceptable_or_unknown"
    if price is not None and invalidation_price is not None and price <= invalidation_price:
        state = "invalidated"
        reason = "Latest price is through the agent thesis invalidation level."
        stock_progress = "invalidation_breached"
    elif candidate_state == "REJECT" or hard_rejects:
        state = "weakening"
        reason = f"Latest deterministic candidate state is {candidate_state}."
        stock_progress = "candidate_rejected"
    elif price is not None and base_target is not None and price >= base_target:
        state = "validated"
        reason = "Latest price is at or above the agent base target."
        stock_progress = "base_target_reached"
    elif option_still_valid:
        state = "pending"
        reason = "Thesis remains pending; deterministic option gates have not invalidated it."
        stock_progress = "tracking"
    else:
        state = "weakening"
        reason = "Thesis lacks a current valid option candidate."
        stock_progress = "option_context_missing"
    evidence_refs = _list_value(thesis.get("evidence_refs"))
    if candidate:
        evidence_refs.append({"type": "candidate_event", "id": candidate.get("event_id")})
    return {
        "validation_id": stable_id("agent_thesis_validation", thesis.get("thesis_id"), candidate_state, price, invalidation_price),
        "thesis_id": thesis.get("thesis_id"),
        "ticker": ticker,
        "validated_at": datetime.utcnow().isoformat(),
        "state": state,
        "reason": reason,
        "option_still_valid": option_still_valid,
        "stock_progress": stock_progress,
        "iv_status": iv_status,
        "candidate_state": candidate_state,
        "evidence_refs": evidence_refs,
        "raw": {
            "price": price,
            "base_target_price": base_target,
            "invalidation_price": invalidation_price,
            "blockers": blockers,
            "hard_rejects": hard_rejects,
            "authority": "deterministic_validation_only",
        },
    }


def first_row(con: Any, sql: str, params: list[Any], json_fields: tuple[str, ...] = ()) -> dict[str, Any] | None:
    rows = query_decoded(con, sql, params, json_fields)
    return rows[0] if rows else None


def query_decoded(con: Any, sql: str, params: list[Any], json_fields: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return [decode_json_fields(row, json_fields) for row in query_rows(con, sql, params)]


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


def _json(value: Any) -> dict[str, Any]:
    decoded = _json_or_value(value)
    return decoded if isinstance(decoded, dict) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _catalyst_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    catalysts: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            catalysts.append({str(key): item.get(key) for key in item if item.get(key) not in (None, "")})
        elif str(item).strip():
            catalysts.append({"type": "unknown", "summary": str(item).strip()})
    return catalysts


def _catalyst_summary(catalysts: list[dict[str, Any]]) -> str:
    summaries = []
    for catalyst in catalysts[:3]:
        label = catalyst.get("type") or catalyst.get("expected_window") or "catalyst"
        watch = catalyst.get("what_to_watch") or catalyst.get("summary") or catalyst.get("description")
        summaries.append(f"{label}: {watch}" if watch else str(label))
    return "; ".join(summaries)


def _invalidation_price(invalidation: list[str]) -> float | None:
    for item in invalidation:
        match = PRICE_RE.search(item)
        if match:
            return _number(match.group(1))
    return None


def _list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        decoded = _json_or_value(value)
        if isinstance(decoded, list):
            return decoded
        return [value] if value else []
    return []


def _date_string(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
