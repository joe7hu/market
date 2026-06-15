"""Auto-split from raw_sources.py — see ARCHITECTURE.md."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from investment_panel.core.db import json_dumps, query_rows
from investment_panel.core.source_ingestion.utils import stable_id

from investment_panel.core.option_agent_thesis.constants import DEFAULT_AGENT_THESIS_REQUEST_LIMIT
from investment_panel.core.option_agent_thesis.dbutil import decode_json_fields, first_row, query_decoded
from investment_panel.core.option_agent_thesis.thesis import attach_agent_theses_to_candidates
from investment_panel.core.option_agent_thesis.validation import refresh_agent_thesis_validations


def refresh_option_agent_work(con: Any, *, strategy_version: str, limit: int = DEFAULT_AGENT_THESIS_REQUEST_LIMIT) -> dict[str, int]:
    attached_rows = attach_agent_theses_to_candidates(con, strategy_version=strategy_version)
    request_result = refresh_agent_thesis_requests(con, strategy_version=strategy_version, limit=limit)
    validation_rows = refresh_agent_thesis_validations(con, strategy_version=strategy_version)
    return {
        "agent_thesis_requests": request_result["requested"],
        "agent_thesis_requests_superseded": request_result["superseded"],
        "agent_theses_attached": attached_rows,
        "agent_thesis_validations": validation_rows,
    }


def refresh_agent_thesis_requests(con: Any, *, strategy_version: str, limit: int = DEFAULT_AGENT_THESIS_REQUEST_LIMIT) -> dict[str, int]:
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
    selected_event_ids = [str(row.get("event_id")) for row in rows if row.get("event_id")]
    superseded = retire_superseded_agent_thesis_requests(con, strategy_version=strategy_version, selected_event_ids=selected_event_ids)
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
    return {"requested": count, "superseded": superseded}


def retire_superseded_agent_thesis_requests(con: Any, *, strategy_version: str, selected_event_ids: list[str]) -> int:
    """Keep agent usage bounded to the current top-ranked candidate set."""

    if selected_event_ids:
        placeholders = ", ".join("?" for _ in selected_event_ids)
        before = query_rows(
            con,
            f"""
            SELECT count(*) AS count
            FROM agent_thesis_request
            WHERE strategy_version = ?
                  AND status = 'open'
                  AND event_id NOT IN ({placeholders})
            """,
            [strategy_version, *selected_event_ids],
        )[0]["count"]
        con.execute(
            f"""
            UPDATE agent_thesis_request
            SET status = 'superseded'
            WHERE strategy_version = ?
                  AND status = 'open'
                  AND event_id NOT IN ({placeholders})
            """,
            [strategy_version, *selected_event_ids],
        )
        return int(before or 0)

    before = query_rows(
        con,
        """
        SELECT count(*) AS count
        FROM agent_thesis_request
        WHERE strategy_version = ? AND status = 'open'
        """,
        [strategy_version],
    )[0]["count"]
    con.execute(
        """
        UPDATE agent_thesis_request
        SET status = 'superseded'
        WHERE strategy_version = ? AND status = 'open'
        """,
        [strategy_version],
    )
    return int(before or 0)


def build_agent_thesis_request(con: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    ticker = str(candidate.get("ticker") or "").upper()
    context = {
        "candidate_event": decode_json_fields(candidate, ("raw",)),
        "instrument": first_row(
            con,
            """
            SELECT symbol, name, asset_class, sector, industry, category, source
            FROM instruments
            WHERE symbol = ?
            LIMIT 1
            """,
            [ticker],
        ),
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
        "fundamentals": first_row(
            con,
            """
            SELECT symbol, period_end, filing_date, form_type, metrics, source_url
            FROM equity_fundamentals
            WHERE symbol = ?
            ORDER BY filing_date DESC NULLS LAST, period_end DESC NULLS LAST
            LIMIT 1
            """,
            [ticker],
            ("metrics",),
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
        # Full per-ticker bundle: one agent run sees ownership, technicals, our
        # position, the decision grade, and upcoming catalysts — not just options.
        "technicals": first_row(
            con,
            "SELECT symbol, date, features FROM technical_features WHERE symbol = ? ORDER BY date DESC LIMIT 1",
            [ticker],
            ("features",),
        ),
        "ownership_and_disclosures": query_decoded(
            con,
            """
            SELECT source_type, trader_name, filer_name, symbol, event_date, filed_date, action, amount, source_url
            FROM disclosures
            WHERE symbol = ?
            ORDER BY filed_date DESC NULLS LAST, event_date DESC NULLS LAST
            LIMIT 6
            """,
            [ticker],
        ),
        "portfolio_position": first_row(
            con,
            "SELECT symbol, quantity, avg_cost, purchase_date, notes FROM portfolio_positions WHERE symbol = ? LIMIT 1",
            [ticker],
        ),
        "decision": first_row(
            con,
            """
            SELECT symbol, as_of, action_grade, freshness_status, source_cluster,
                   inclusion_reasons, blocking_gates, decision_basis
            FROM symbol_decision_snapshots
            WHERE symbol = ?
            LIMIT 1
            """,
            [ticker],
            ("inclusion_reasons", "blocking_gates", "decision_basis"),
        ),
        "catalysts": query_decoded(
            con,
            """
            SELECT symbol, event_date, event, expected_impact, importance, source
            FROM catalysts
            WHERE symbol = ?
            ORDER BY event_date ASC NULLS LAST
            LIMIT 5
            """,
            [ticker],
        ),
        "earnings": query_decoded(
            con,
            "SELECT symbol, event_date, event_type FROM earnings_events WHERE symbol = ? ORDER BY event_date ASC NULLS LAST LIMIT 3",
            [ticker],
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
        "raw": {
            "authority": "hypothesis_only",
            "required_output": "agent_thesis",
            "queue_policy": "current_top_ranked_candidates_only",
        },
    }


def agent_thesis_prompt(ticker: str) -> str:
    return (
        f"Generate a structured options thesis for {ticker}. "
        "Return JSON only with keys: ticker, bull_target_price, bull_target_date, "
        "base_target_price, core_thesis, required_proofs, catalysts, invalidation, "
        "bear_case, confidence, evidence_refs. Agents create hypotheses only; "
        "deterministic code validates proofs, catalysts, invalidation, options math, and state. "
        "The core_thesis must be product-and-technology grounded, not chart-only: explain the "
        "business or protocol mechanism, the technology adoption trend, and a falsifiable 12-24 "
        "month prediction that can drive the stock toward the bull target. Required proofs should "
        "be product, customer, revenue, margin, adoption, regulatory, or ecosystem evidence; do not "
        "use price action or option Greeks as proof. Use supplied source/news/fundamental evidence "
        "refs where available and state missing evidence as a risk in bear_case or invalidation. "
        "Do not recommend or execute trades."
    )
