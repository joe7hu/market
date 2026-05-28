from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from investment_panel.core.db import db, init_db
from investment_panel.core.thesis_monitor import thesis_monitor_rows


def test_thesis_monitor_flags_stale_owned_position_near_invalidation(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    reviewed_at = datetime.now(UTC) - timedelta(days=60)
    now = datetime.now(UTC)

    with db(db_path) as con:
        con.execute("INSERT INTO portfolio_positions VALUES ('NVDA', 10, 90, current_date, 'core AI position')")
        con.execute(
            "INSERT INTO theses VALUES ('NVDA', ?, ?)",
            [
                json.dumps(
                    {
                        "position_status": "owned",
                        "core_thesis": "AI accelerator leader with durable datacenter demand.",
                        "why_owned": "Owned for AI infrastructure exposure.",
                        "invalidation": "Below $95 the setup no longer supports the thesis.",
                        "invalidation_price": 95,
                        "evidence_links": ["https://example.com/nvda-thesis"],
                        "last_reviewed": reviewed_at.isoformat(),
                    }
                ),
                reviewed_at.isoformat(),
            ],
        )
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('NVDA', ?, 101, 0.5, 0.5, 'USD', 'test_quote', '{}')",
            [now.isoformat()],
        )
        con.execute(
            """
            INSERT INTO decision_queue (
                symbol, as_of, rank, action_grade, decision_bucket, score,
                discovery_score, decision_score, action_score, freshness_status,
                quote_freshness, daily_analysis_freshness, filing_freshness,
                thesis_freshness, overall_decision_freshness, source_cluster,
                evidence_count, raw_source_rows, independent_source_count,
                evidence_items_count, primary_evidence_count, inclusion_reasons,
                blocking_gates, decision_basis, latest_quote, latest_quote_at,
                latest_observed_at, next_event_at, catalyst_window, liquidity_grade,
                portfolio_impact, invalidation
            )
            VALUES (
                'NVDA', ?, 1, 'Stale', 'Stale', 42,
                70, 70, 42, 'stale',
                'fresh', 'stale', 'not_applicable',
                'stale', 'stale', 'technical',
                3, 5, 2,
                3, 1, '["owned portfolio row"]',
                '["stale_daily_analysis"]', '{"summary":"stale daily analysis"}', 101, ?,
                ?, NULL, '-', 'A',
                '{"owned":true}', 'Refresh data'
            )
            """,
            [now.isoformat(), now.isoformat(), now.isoformat()],
        )

        rows = thesis_monitor_rows(con, [{"symbol": "NVDA"}])

    nvda = next(row for row in rows if row["symbol"] == "NVDA")
    assert nvda["owned"] is True
    assert nvda["watched"] is True
    assert nvda["thesis"].startswith("AI accelerator leader")
    assert nvda["why_owned_watched"] == "Owned for AI infrastructure exposure."
    assert nvda["invalidation_price"] == 95
    assert nvda["stale_thesis"] is True
    assert "last reviewed" in nvda["stale_reason"]
    assert "invalidation_near" in nvda["contradiction_flags"]
    assert "owned_position_decision_stale" in nvda["contradiction_flags"]
    assert nvda["needs_review"] is True
    assert "near the stored invalidation level" in nvda["review_reason"]


def test_thesis_monitor_creates_watchlist_audit_row_without_thesis(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)

    with db(db_path) as con:
        rows = thesis_monitor_rows(con, [{"symbol": "MU"}])

    mu = next(row for row in rows if row["symbol"] == "MU")
    assert mu["watched"] is True
    assert mu["owned"] is False
    assert mu["status"] == "watched"
    assert mu["stale_thesis"] is True
    assert "missing thesis" in mu["stale_reason"]
    assert mu["needs_review"] is True
