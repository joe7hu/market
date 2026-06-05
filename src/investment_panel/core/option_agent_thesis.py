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
        "bear_case, confidence, evidence_refs. Agents create hypotheses only; "
        "deterministic code validates proofs, catalysts, invalidation, options math, and state. "
        "Do not recommend or execute trades."
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
        thesis = decode_json_fields(thesis, ("required_proofs", "invalidation_conditions", "catalysts", "evidence_refs", "raw"))
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
        source_signals = query_decoded(
            con,
            """
            SELECT source_item_id, source_id, observed_at, signal_type, sentiment,
                   direction, confidence, thesis, antithesis, catalysts, risks,
                   invalidation, evidence_refs
            FROM ticker_source_signals
            WHERE symbol = ?
            ORDER BY observed_at DESC, confidence DESC NULLS LAST
            LIMIT 12
            """,
            [thesis["ticker"]],
            ("catalysts", "risks", "evidence_refs"),
        )
        dated_catalysts = query_decoded(
            con,
            """
            SELECT id, event_date, event, expected_impact, source,
                   verification_status, source_url, raw
            FROM catalysts
            WHERE symbol = ?
            ORDER BY event_date ASC NULLS LAST
            LIMIT 8
            """,
            [thesis["ticker"]],
            ("raw",),
        )
        news = query_decoded(
            con,
            """
            SELECT id, published_at, provider, title, related_symbols, link, source
            FROM news_items
            WHERE contains(CAST(related_symbols AS VARCHAR), ?)
            ORDER BY published_at DESC
            LIMIT 8
            """,
            [thesis["ticker"]],
            ("related_symbols",),
        )
        fundamentals = first_row(
            con,
            """
            SELECT symbol, period_end, filing_date, form_type, metrics, source_url
            FROM equity_fundamentals
            WHERE symbol = ?
            ORDER BY filing_date DESC NULLS LAST, period_end DESC NULLS LAST
            LIMIT 1
            """,
            [thesis["ticker"]],
            ("metrics",),
        )
        validation = build_agent_thesis_validation(
            thesis,
            candidate,
            stock,
            source_signals,
            dated_catalysts,
            news,
            fundamentals,
            strategy_version=strategy_version,
        )
        con.execute(
            """
            INSERT OR REPLACE INTO agent_thesis_validation
            (validation_id, thesis_id, ticker, strategy_version, validation_date,
             candidate_event_id, candidate_snapshot_time, validated_at, state, reason,
             option_still_valid, stock_progress, iv_status, candidate_state,
             proof_status, catalyst_status, invalidation_status, evidence_status,
             red_team_status, red_team_flags, evidence_refs, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                validation["validation_id"],
                validation["thesis_id"],
                validation["ticker"],
                validation["strategy_version"],
                validation["validation_date"],
                validation["candidate_event_id"],
                validation["candidate_snapshot_time"],
                validation["validated_at"],
                validation["state"],
                validation["reason"],
                validation["option_still_valid"],
                validation["stock_progress"],
                validation["iv_status"],
                validation["candidate_state"],
                validation["proof_status"],
                validation["catalyst_status"],
                validation["invalidation_status"],
                validation["evidence_status"],
                validation["red_team_status"],
                json_dumps(validation["red_team_flags"]),
                json_dumps(validation["evidence_refs"]),
                json_dumps(validation["raw"]),
            ],
        )
        count += 1
    return count


def build_agent_thesis_validation(
    thesis: dict[str, Any],
    candidate: dict[str, Any] | None,
    stock: dict[str, Any] | None,
    source_signals: list[dict[str, Any]] | None = None,
    dated_catalysts: list[dict[str, Any]] | None = None,
    news: list[dict[str, Any]] | None = None,
    fundamentals: dict[str, Any] | None = None,
    *,
    strategy_version: str = "unknown",
) -> dict[str, Any]:
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
    candidate_event_id = (candidate or {}).get("event_id")
    candidate_snapshot_time = _iso_or_none((candidate or {}).get("snapshot_time"))
    as_of_date = _date_value(candidate_snapshot_time) or _date_value((stock or {}).get("snapshot_time")) or date.today()
    proof_check = _proof_check(_string_list(thesis.get("required_proofs")), source_signals or [], news or [])
    catalyst_check = _catalyst_check(_catalyst_list(thesis.get("catalysts")), dated_catalysts or [], source_signals or [], news or [], as_of_date)
    evidence_status = _evidence_status(_list_value(thesis.get("evidence_refs")), source_signals or [], news or [])
    red_team_check = _red_team_check(thesis, candidate, stock, source_signals or [], news or [], fundamentals)
    invalidation_status = "missing"
    if price is not None and invalidation_price is not None and price <= invalidation_price:
        state = "invalidated"
        reason = "Latest price is through the agent thesis invalidation level."
        stock_progress = "invalidation_breached"
        invalidation_status = "breached"
    elif candidate_state == "REJECT" or hard_rejects:
        state = "weakening"
        reason = f"Latest deterministic candidate state is {candidate_state}."
        stock_progress = "candidate_rejected"
        invalidation_status = "clear" if invalidation_price is not None else "missing"
    elif price is not None and base_target is not None and price >= base_target:
        state = "validated"
        reason = "Latest price is at or above the agent base target."
        stock_progress = "base_target_reached"
        invalidation_status = "clear" if invalidation_price is not None else "missing"
    elif option_still_valid:
        state = "pending"
        reason = "Thesis remains pending; deterministic option gates have not invalidated it."
        stock_progress = "tracking"
        invalidation_status = "clear" if invalidation_price is not None else "missing"
    else:
        state = "weakening"
        reason = "Thesis lacks a current valid option candidate."
        stock_progress = "option_context_missing"
        invalidation_status = "clear" if invalidation_price is not None else "missing"
    if proof_check["status"] == "missing":
        reason = f"{reason} Required proof list is missing."
    elif proof_check["status"] == "pending" and state == "validated":
        state = "pending"
        reason = "Price reached the base target, but required proofs are not source-backed yet."
    evidence_refs = _list_value(thesis.get("evidence_refs"))
    if candidate:
        evidence_refs.append({"type": "candidate_event", "id": candidate.get("event_id")})
    for signal in (source_signals or [])[:3]:
        if signal.get("source_item_id"):
            evidence_refs.append({"type": "ticker_source_signal", "id": signal.get("source_item_id")})
    for catalyst in (dated_catalysts or [])[:2]:
        if catalyst.get("id"):
            evidence_refs.append({"type": "catalyst", "id": catalyst.get("id")})
    return {
        "validation_id": stable_id(
            "agent_thesis_validation",
            thesis.get("thesis_id"),
            strategy_version,
            candidate_event_id,
            as_of_date.isoformat(),
        ),
        "thesis_id": thesis.get("thesis_id"),
        "ticker": ticker,
        "strategy_version": strategy_version,
        "validation_date": as_of_date.isoformat(),
        "candidate_event_id": candidate_event_id,
        "candidate_snapshot_time": candidate_snapshot_time,
        "validated_at": datetime.utcnow().isoformat(),
        "state": state,
        "reason": reason,
        "option_still_valid": option_still_valid,
        "stock_progress": stock_progress,
        "iv_status": iv_status,
        "candidate_state": candidate_state,
        "proof_status": proof_check["status"],
        "catalyst_status": catalyst_check["status"],
        "invalidation_status": invalidation_status,
        "evidence_status": evidence_status,
        "red_team_status": red_team_check["status"],
        "red_team_flags": red_team_check["flags"],
        "evidence_refs": evidence_refs,
        "raw": {
            "price": price,
            "base_target_price": base_target,
            "invalidation_price": invalidation_price,
            "as_of_date": as_of_date.isoformat(),
            "strategy_version": strategy_version,
            "candidate_event_id": candidate_event_id,
            "candidate_snapshot_time": candidate_snapshot_time,
            "proof_check": proof_check,
            "catalyst_check": catalyst_check,
            "red_team_check": red_team_check,
            "evidence_status": evidence_status,
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
        decoded = _json_or_value(value)
        if isinstance(decoded, list):
            return _string_list(decoded)
        if decoded != value:
            return _string_list(decoded)
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _catalyst_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        decoded = _json_or_value(value)
        if decoded != value:
            return _catalyst_list(decoded)
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


def _proof_check(required_proofs: list[str], source_signals: list[dict[str, Any]], news: list[dict[str, Any]]) -> dict[str, Any]:
    if not required_proofs:
        return {"status": "missing", "matched": [], "missing": [], "match_count": 0, "required_count": 0}
    corpus = _evidence_corpus(source_signals, news)
    matched: list[str] = []
    missing: list[str] = []
    for proof in required_proofs:
        tokens = _content_tokens(proof)
        if tokens and any(token in corpus for token in tokens):
            matched.append(proof)
        else:
            missing.append(proof)
    if len(matched) == len(required_proofs):
        status = "supported"
    elif matched:
        status = "partial"
    else:
        status = "pending"
    return {
        "status": status,
        "matched": matched,
        "missing": missing,
        "match_count": len(matched),
        "required_count": len(required_proofs),
    }


def _catalyst_check(
    thesis_catalysts: list[dict[str, Any]],
    dated_catalysts: list[dict[str, Any]],
    source_signals: list[dict[str, Any]],
    news: list[dict[str, Any]],
    as_of_date: date,
) -> dict[str, Any]:
    if not thesis_catalysts:
        return {"status": "missing", "matched": [], "scheduled": []}
    scheduled = [
        catalyst
        for catalyst in dated_catalysts
        if (event_date := _date_value(catalyst.get("event_date"))) is not None and event_date >= as_of_date
    ]
    if scheduled:
        return {
            "status": "scheduled",
            "matched": [str(item.get("event") or item.get("event_kind") or item.get("id")) for item in scheduled[:3]],
            "scheduled": [str(item.get("event_date")) for item in scheduled[:3]],
        }
    corpus = _evidence_corpus(source_signals, news)
    matched: list[str] = []
    for catalyst in thesis_catalysts:
        text = " ".join(str(catalyst.get(key) or "") for key in ("type", "expected_window", "what_to_watch", "summary", "description"))
        tokens = _content_tokens(text)
        if tokens and any(token in corpus for token in tokens):
            matched.append(text.strip())
    if matched:
        return {"status": "source_confirmed", "matched": matched[:3], "scheduled": []}
    return {"status": "pending", "matched": [], "scheduled": []}


def _evidence_status(evidence_refs: list[Any], source_signals: list[dict[str, Any]], news: list[dict[str, Any]]) -> str:
    if evidence_refs and source_signals:
        return "source_backed"
    if evidence_refs:
        return "agent_cited"
    if source_signals:
        return "source_context_available"
    if news:
        return "news_only"
    return "missing"


def _red_team_check(
    thesis: dict[str, Any],
    candidate: dict[str, Any] | None,
    stock: dict[str, Any] | None,
    source_signals: list[dict[str, Any]],
    news: list[dict[str, Any]],
    fundamentals: dict[str, Any] | None,
) -> dict[str, Any]:
    bear_case = str(thesis.get("bear_case") or "").strip()
    if not bear_case:
        return {"status": "missing", "flags": [], "source_matches": [], "hard_checks": [], "bear_case": ""}

    raw_candidate = _json(candidate.get("raw")) if candidate else {}
    blockers = [str(item) for item in raw_candidate.get("blockers") or []]
    hard_rejects = [str(item) for item in raw_candidate.get("hard_rejects") or []]
    candidate_reasons = [*blockers, *hard_rejects]
    stock_raw = _json((stock or {}).get("raw"))
    metrics = _json((fundamentals or {}).get("metrics"))
    flags: list[dict[str, Any]] = []

    for reason in candidate_reasons:
        reason_text = reason.lower()
        if any(token in reason_text for token in ("spread", "open_interest", "volume", "liquidity")):
            flags.append({"type": "option_liquidity_risk", "evidence": reason})
        if "iv" in reason_text:
            flags.append({"type": "iv_overpricing_risk", "evidence": reason})
        if "stock_below_50d" in reason_text or "rs_vs_qqq_20d_negative" in reason_text:
            flags.append({"type": "technical_downtrend_risk", "evidence": reason})

    price = _number((stock or {}).get("price"))
    ma_50 = _number((stock or {}).get("ma_50"))
    ma_200 = _number((stock or {}).get("ma_200"))
    rs_20 = _number((stock or {}).get("rs_vs_qqq_20d"))
    if price is not None and ma_50 is not None and price < ma_50:
        flags.append({"type": "technical_downtrend_risk", "evidence": "price_below_50d"})
    if price is not None and ma_200 is not None and price < ma_200:
        flags.append({"type": "long_term_downtrend_risk", "evidence": "price_below_200d"})
    if rs_20 is not None and rs_20 < 0:
        flags.append({"type": "relative_strength_risk", "evidence": "rs_vs_qqq_20d_negative"})

    free_cash_flow = _metric_number(metrics, "free_cash_flow", "freeCashflow", "free_cashflow")
    operating_cash_flow = _metric_number(metrics, "operating_cash_flow", "operatingCashflow", "totalCashFromOperatingActivities")
    cash = _metric_number(metrics, "cash", "total_cash", "totalCash")
    debt = _metric_number(metrics, "total_debt", "totalDebt", "debt")
    liabilities = _metric_number(metrics, "liabilities", "total_liabilities", "totalLiabilities")
    assets = _metric_number(metrics, "assets", "total_assets", "totalAssets")
    revenue_growth = _metric_number(metrics, "revenue_growth", "revenueGrowth", "revenue_growth_yoy")
    if free_cash_flow is not None and free_cash_flow < 0:
        flags.append({"type": "cash_burn_risk", "evidence": "negative_free_cash_flow"})
    elif operating_cash_flow is not None and operating_cash_flow < 0:
        flags.append({"type": "cash_burn_risk", "evidence": "negative_operating_cash_flow"})
    if cash is not None and free_cash_flow is not None and free_cash_flow < 0 and cash < abs(free_cash_flow):
        flags.append({"type": "cash_runway_risk", "evidence": "cash_less_than_one_year_negative_fcf"})
    if debt is not None and cash is not None and debt > cash * 2:
        flags.append({"type": "balance_sheet_risk", "evidence": "debt_more_than_2x_cash"})
    if liabilities is not None and assets is not None and assets > 0 and liabilities / assets > 0.7:
        flags.append({"type": "balance_sheet_risk", "evidence": "liabilities_above_70pct_assets"})
    if revenue_growth is not None and revenue_growth < 0:
        flags.append({"type": "growth_deceleration_risk", "evidence": "negative_revenue_growth"})

    bear_tokens = _content_tokens(bear_case)
    risk_corpus = _risk_corpus(source_signals, news)
    source_matches = sorted(token for token in bear_tokens if token in risk_corpus)[:12]
    hard_flags = _dedupe_flags(flags)
    if hard_flags:
        status = "hard_risk_triggered"
    elif source_matches:
        status = "source_backed"
    else:
        status = "agent_only"
    return {
        "status": status,
        "flags": hard_flags,
        "source_matches": source_matches,
        "hard_checks": {
            "stock": {
                "price": price,
                "ma_50": ma_50,
                "ma_200": ma_200,
                "rs_vs_qqq_20d": rs_20,
                "raw": stock_raw,
            },
            "fundamentals": {
                "free_cash_flow": free_cash_flow,
                "operating_cash_flow": operating_cash_flow,
                "cash": cash,
                "total_debt": debt,
                "liabilities": liabilities,
                "assets": assets,
                "revenue_growth": revenue_growth,
            },
            "candidate_reasons": candidate_reasons,
        },
        "bear_case": bear_case,
    }


def _risk_corpus(source_signals: list[dict[str, Any]], news: list[dict[str, Any]]) -> set[str]:
    texts: list[str] = []
    for signal in source_signals:
        texts.extend(
            [
                str(signal.get("signal_type") or ""),
                str(signal.get("antithesis") or ""),
                str(signal.get("invalidation") or ""),
                json.dumps(signal.get("risks") or ""),
            ]
        )
    for item in news:
        texts.extend([str(item.get("title") or ""), str(item.get("provider") or ""), str(item.get("source") or "")])
    return {token for text in texts for token in _content_tokens(text)}


def _dedupe_flags(flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for flag in flags:
        key = (str(flag.get("type") or ""), str(flag.get("evidence") or ""))
        if key in seen:
            continue
        seen.add(key)
        output.append(flag)
    return output


def _evidence_corpus(source_signals: list[dict[str, Any]], news: list[dict[str, Any]]) -> set[str]:
    texts: list[str] = []
    for signal in source_signals:
        texts.extend(
            [
                str(signal.get("signal_type") or ""),
                str(signal.get("thesis") or ""),
                str(signal.get("antithesis") or ""),
                str(signal.get("invalidation") or ""),
                json.dumps(signal.get("catalysts") or ""),
                json.dumps(signal.get("risks") or ""),
            ]
        )
    for item in news:
        texts.extend([str(item.get("title") or ""), str(item.get("provider") or ""), str(item.get("source") or "")])
    return {token for text in texts for token in _content_tokens(text)}


STOP_WORDS = {
    "about",
    "after",
    "before",
    "below",
    "consecutive",
    "expected",
    "growth",
    "improve",
    "improves",
    "next",
    "quarter",
    "quarters",
    "recover",
    "recovers",
    "related",
    "should",
    "stock",
    "target",
    "watch",
    "without",
}


def _content_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(text).lower())
        if len(token) >= 4 and token not in STOP_WORDS
    }


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


def _iso_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _metric_number(metrics: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(metrics.get(key))
        if value is not None:
            return value
    return None
