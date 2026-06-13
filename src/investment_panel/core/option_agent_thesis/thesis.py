"""Auto-split from raw_sources.py — see ARCHITECTURE.md."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from investment_panel.core.db import json_dumps, query_rows
from investment_panel.core.source_ingestion.utils import stable_id

from investment_panel.core.option_agent_thesis.coerce import _catalyst_list, _catalyst_summary, _confidence_score, _date_string, _list_value, _number, _string_list
from investment_panel.core.option_agent_thesis.constants import AGENT_THESIS_VERSION, AgentThesisValidationError


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
    confidence = _confidence_score(payload.get("confidence"))
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
        "confidence": confidence,
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
