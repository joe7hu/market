"""Decision read-model persistence."""

from __future__ import annotations
from typing import Any
from investment_panel.core.db import json_dumps, query_rows, upsert_instrument



def persist_discovered_universe(con: Any, rows: list[dict[str, Any]]) -> None:
    con.execute("DELETE FROM discovered_universe")
    for row in rows:
        con.execute(
            """
            INSERT OR REPLACE INTO discovered_universe
            (symbol, name, asset_class, inclusion_reasons, source_counts, latest_source_timestamp,
             latest_observed_at, next_event_at, eligibility_status, eligibility_detail, evidence_score,
             discovery_score, liquidity_score, recency_score,
             universe_rank, decision_universe_member, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row["symbol"], row.get("name"), row.get("asset_class"), json_dumps(row.get("inclusion_reasons") or []),
                json_dumps(row.get("source_counts") or {}), row.get("latest_source_timestamp"),
                row.get("latest_observed_at"), row.get("next_event_at"),
                row.get("eligibility_status"), row.get("eligibility_detail"), row.get("evidence_score"),
                row.get("discovery_score"), row.get("liquidity_score"), row.get("recency_score"), row.get("universe_rank"),
                row.get("decision_universe_member"), row.get("updated_at"),
            ],
        )




def persist_decision_queue(con: Any, rows: list[dict[str, Any]]) -> None:
    con.execute("DELETE FROM decision_queue")
    for row in rows:
        con.execute(
            """
            INSERT OR REPLACE INTO decision_queue
            (symbol, as_of, rank, action_grade, decision_bucket, score, discovery_score, decision_score,
             action_score, freshness_status, quote_freshness, daily_analysis_freshness, filing_freshness,
             thesis_freshness, overall_decision_freshness, source_cluster, evidence_count, raw_source_rows,
             independent_source_count, evidence_items_count, primary_evidence_count, inclusion_reasons,
             blocking_gates, decision_basis, latest_quote, latest_quote_at, latest_observed_at, next_event_at,
             catalyst_window, liquidity_grade, portfolio_impact, invalidation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row["symbol"], row.get("as_of"), row.get("rank"), row.get("action_grade"), row.get("decision_bucket"),
                row.get("score"), row.get("discovery_score"), row.get("decision_score"), row.get("action_score"),
                row.get("freshness_status"), row.get("quote_freshness"), row.get("daily_analysis_freshness"),
                row.get("filing_freshness"), row.get("thesis_freshness"), row.get("overall_decision_freshness"),
                row.get("source_cluster"), row.get("evidence_count"), row.get("raw_source_rows"),
                row.get("independent_source_count"), row.get("evidence_items_count"), row.get("primary_evidence_count"),
                json_dumps(row.get("inclusion_reasons") or []), json_dumps(row.get("blocking_gates") or []),
                json_dumps(row.get("decision_basis") or {}), row.get("latest_quote"), row.get("latest_quote_at"),
                row.get("latest_observed_at"), row.get("next_event_at"), row.get("catalyst_window"), row.get("liquidity_grade"), json_dumps(row.get("portfolio_impact") or {}),
                row.get("invalidation"),
            ],
        )




def persist_source_freshness(con: Any, rows: list[dict[str, Any]]) -> None:
    con.execute("DELETE FROM source_freshness")
    for row in rows:
        con.execute(
            """
            INSERT OR REPLACE INTO source_freshness
            (source_key, source_type, provider, last_observed_at, freshness_status, stale_after,
             status, detail, docs_only, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row["source_key"], row.get("source_type"), row.get("provider"), row.get("last_observed_at"),
                row.get("freshness_status"), row.get("stale_after"), row.get("provider_status") or row.get("status"),
                row.get("detail"), row.get("docs_only"), row.get("checked_at"),
            ],
        )




def persist_symbol_decision_snapshots(con: Any, rows: list[dict[str, Any]]) -> None:
    con.execute("DELETE FROM symbol_decision_snapshots")
    for row in rows:
        con.execute(
            """
            INSERT OR REPLACE INTO symbol_decision_snapshots
            (symbol, as_of, action_grade, freshness_status, quote_freshness, daily_analysis_freshness,
             filing_freshness, thesis_freshness, source_cluster, inclusion_reasons,
             blocking_gates, decision_basis, snapshot)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row["symbol"], row.get("as_of"), row.get("action_grade"), row.get("freshness_status"),
                row.get("quote_freshness"), row.get("daily_analysis_freshness"), row.get("filing_freshness"),
                row.get("thesis_freshness"), row.get("source_cluster"), json_dumps(row.get("inclusion_reasons") or []),
                json_dumps(row.get("blocking_gates") or []), json_dumps(row.get("decision_basis") or {}),
                json_dumps(row.get("snapshot") or {}),
            ],
        )
