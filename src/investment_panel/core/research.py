"""Research packet and deterministic memo generation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from investment_panel.core.db import json_dumps, query_rows


def build_research_packet(con: Any, symbol: str) -> dict[str, Any]:
    candidate = first_row(con, "SELECT * FROM candidates WHERE symbol = ? ORDER BY run_date DESC, score DESC LIMIT 1", [symbol])
    instrument = first_row(con, "SELECT * FROM instruments WHERE symbol = ?", [symbol])
    technical = first_row(con, "SELECT * FROM technical_features WHERE symbol = ? ORDER BY date DESC LIMIT 1", [symbol])
    prices = query_rows(con, "SELECT * FROM prices_daily WHERE symbol = ? ORDER BY date DESC LIMIT 90", [symbol])
    theses = query_rows(con, "SELECT * FROM birdclaw_theses WHERE symbol = ? ORDER BY created_at DESC LIMIT 12", [symbol])
    disclosures = query_rows(con, "SELECT * FROM disclosures WHERE symbol = ? ORDER BY filed_date DESC LIMIT 12", [symbol])
    portfolio = first_row(con, "SELECT * FROM portfolio_positions WHERE symbol = ?", [symbol])
    existing_thesis = first_row(con, "SELECT * FROM theses WHERE symbol = ?", [symbol])
    return {
        "symbol": symbol,
        "created_at": datetime.utcnow().isoformat(),
        "instrument": instrument,
        "candidate": decode_json_fields(candidate, ("score_breakdown", "evidence")),
        "technical": decode_json_fields(technical, ("features",)),
        "prices_recent": prices,
        "arco_thesis_evidence": [decode_json_fields(row, ("claims", "engagement")) for row in theses],
        "disclosures": [decode_json_fields(row, ("raw",)) for row in disclosures],
        "portfolio_position": portfolio,
        "existing_thesis": decode_json_fields(existing_thesis, ("thesis_json",)),
    }


def write_packet(packet: dict[str, Any], packet_dir: Path) -> Path:
    packet_dir.mkdir(parents=True, exist_ok=True)
    path = packet_dir / f"{packet['symbol']}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.json"
    path.write_text(json_dumps(packet), encoding="utf-8")
    return path


def generate_deterministic_memo(packet: dict[str, Any]) -> dict[str, Any]:
    candidate = packet.get("candidate") or {}
    score = candidate.get("score")
    decision = candidate.get("decision") or "watch"
    technical = (packet.get("technical") or {}).get("features") or {}
    evidence = packet.get("arco_thesis_evidence") or []
    conviction = "high" if score and score >= 80 else "medium" if score and score >= 65 else "low"
    why_now = []
    if technical.get("technical_score", 0) >= 70:
        why_now.append("Technical setup is constructive based on latest stored trend features.")
    if evidence:
        why_now.append(f"{len(evidence)} Arco/Birdclaw thesis evidence items are attached to the packet.")
    if not why_now:
        why_now.append("Evidence is not yet strong enough for an action signal.")
    report_json = {
        "symbol": packet["symbol"],
        "decision": decision,
        "conviction": conviction,
        "why_now": why_now,
        "bull_case": ["Evidence cluster and price action may be pointing at an under-reviewed opportunity."],
        "bear_case": ["The packet may be source-thin, stale, or mostly momentum-driven; require primary evidence before acting."],
        "invalidation": ["Thesis evidence fails to connect to fundamentals, category trend, or a concrete catalyst."],
        "entry_plan": {
            "ideal_entry": "Wait for a defined pullback/retest or primary-source catalyst confirmation.",
            "bad_entry": "Do not chase an extended move without fresh evidence.",
            "first_review_date": "next weekly review",
        },
        "position_sizing": {
            "suggested_max_weight": "small until thesis is primary-source verified",
            "initial_weight": "watchlist or starter only",
        },
        "portfolio_impact": {
            "position_exists": bool(packet.get("portfolio_position")),
            "note": "Check overlap with current category and crypto/AI beta exposure.",
        },
        "evidence": packet.get("candidate", {}).get("evidence") or [],
    }
    markdown = memo_markdown(packet, report_json)
    return {"markdown": markdown, "json": report_json}


def memo_markdown(packet: dict[str, Any], report: dict[str, Any]) -> str:
    lines = [
        f"# {packet['symbol']} Research Memo",
        "",
        "## Verdict",
        f"Decision: {report['decision']}",
        f"Conviction: {report['conviction']}",
        "Time horizon: Weeks to months",
        "",
        "## Why Now",
    ]
    lines.extend(f"- {item}" for item in report["why_now"])
    lines.extend(["", "## Evidence"])
    for item in (packet.get("arco_thesis_evidence") or [])[:6]:
        lines.append(f"- Arco/Birdclaw: {item.get('thesis_summary') or 'Evidence item'} ({item.get('source_url') or 'local packet'})")
    if not packet.get("arco_thesis_evidence"):
        lines.append("- No Arco/Birdclaw thesis evidence found for this symbol.")
    lines.extend(
        [
            "",
            "## Bull Case",
            f"- {report['bull_case'][0]}",
            "",
            "## Bear Case",
            f"- {report['bear_case'][0]}",
            "",
            "## What Would Make Me Wrong",
            f"- {report['invalidation'][0]}",
            "",
            "## Entry Plan",
            f"- Ideal entry: {report['entry_plan']['ideal_entry']}",
            f"- Bad entry / chase zone: {report['entry_plan']['bad_entry']}",
            "",
            "## Position Sizing",
            f"- Suggested max weight: {report['position_sizing']['suggested_max_weight']}",
            f"- Initial weight: {report['position_sizing']['initial_weight']}",
        ]
    )
    return "\n".join(lines) + "\n"


def store_report(con: Any, symbol: str, report_type: str, report: dict[str, Any], evidence: dict[str, Any]) -> str:
    report_id = stable_id(f"{symbol}:{report_type}:{datetime.utcnow().isoformat()}")
    con.execute(
        """
        INSERT INTO research_reports
        (id, symbol, created_at, report_type, report_markdown, report_json, evidence)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            report_id,
            symbol,
            datetime.utcnow().isoformat(),
            report_type,
            report["markdown"],
            json_dumps(report["json"]),
            json_dumps(evidence),
        ],
    )
    return report_id


def first_row(con: Any, sql: str, params: list[Any]) -> dict[str, Any] | None:
    rows = query_rows(con, sql, params)
    return rows[0] if rows else None


def decode_json_fields(row: dict[str, Any] | None, fields: tuple[str, ...]) -> dict[str, Any] | None:
    if row is None:
        return None
    decoded = dict(row)
    for field in fields:
        if field in decoded:
            try:
                decoded[field] = json.loads(decoded[field]) if decoded[field] else None
            except Exception:
                pass
    return decoded


def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

