"""Research read accessors: deterministic research packets, stored reports, trader profiles."""

from __future__ import annotations
from pathlib import Path
from typing import Any
from investment_panel.core.db import query_rows
from investment_panel.core.research import build_research_packet, generate_deterministic_memo

from investment_panel.core.panel.coerce import decode_fields, first_heading
from investment_panel.core.panel.disclosures import _compact_empty_fields



def research_packets(con: Any) -> list[dict[str, Any]]:
    symbols = [
        str(row.get("symbol") or "").upper()
        for row in query_rows(
            con,
            """
            SELECT symbol
            FROM candidates
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY run_date DESC, score DESC) = 1
            ORDER BY score DESC
            LIMIT 25
            """,
        )
    ]
    packets: list[dict[str, Any]] = []
    for symbol in symbols:
        if not symbol:
            continue
        packet = build_research_packet(con, symbol)
        if not packet.get("candidate"):
            continue
        memo = generate_deterministic_memo(packet)
        report = memo.get("json") or {}
        packets.append(
            {
                "symbol": symbol,
                "created_at": packet.get("created_at"),
                "decision": report.get("decision"),
                "conviction": report.get("conviction"),
                "why_now": report.get("why_now"),
                "bull_case": report.get("bull_case"),
                "bear_case": report.get("bear_case"),
                "invalidation": report.get("invalidation"),
                "entry_plan": report.get("entry_plan"),
                "position_sizing": report.get("position_sizing"),
                "portfolio_impact": report.get("portfolio_impact"),
                "evidence_count": len(packet.get("arco_thesis_evidence") or []),
                "price_rows": len(packet.get("prices_recent") or []),
                "has_position": bool(packet.get("portfolio_position")),
            }
        )
    return packets




def reports(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, symbol, created_at, report_type, report_markdown, report_json, evidence
        FROM research_reports
        ORDER BY created_at DESC
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("report_json", "evidence")) for row in rows]




def trader_profiles(profile_dir: Path) -> list[dict[str, Any]]:
    if not profile_dir.exists():
        return []
    rows = []
    for path in sorted(profile_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        rows.append({"id": path.stem, "name": first_heading(text) or path.stem, "profile_markdown": text})
    return rows
