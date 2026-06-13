"""Trade journal: capture opportunity at click for predicted-vs-realized (Phase 4)."""

from __future__ import annotations

from investment_panel.core.options_radar import (
    DEFAULT_STRATEGY_VERSION,
    record_trade_journal_entry,
)


def test_record_journal_entry_captures_snapshot(tmp_path):
    from investment_panel.core.db import db, init_db, query_rows

    init_db(tmp_path / "j.duckdb")
    opportunity = {
        "ticker": "NVDA",
        "premium_mid": 5.5,
        "conviction_score": 81.0,
        "raw": {"primary_detail": {"ev_multiple": 3.2, "calibrated_p2x": 0.42}},
    }
    with db(tmp_path / "j.duckdb") as con:
        journal_id = record_trade_journal_entry(
            con,
            ticker="NVDA",
            contract_id="NVDA_C1",
            event_id="ev1",
            strategy_version=DEFAULT_STRATEGY_VERSION,
            opportunity=opportunity,
            notes="entered half size",
        )
        rows = query_rows(con, "SELECT * FROM trade_journal WHERE journal_id = ?", [journal_id])

    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "NVDA"
    assert row["entry_premium"] == 5.5
    assert row["predicted_ev_multiple"] == 3.2
    assert row["predicted_p2x"] == 0.42  # prefers calibrated
    assert row["conviction_score"] == 81.0
    assert row["realized_status"] == "open"
    assert row["notes"] == "entered half size"


def test_record_journal_entry_tolerates_sparse_opportunity(tmp_path):
    from investment_panel.core.db import db, init_db, query_rows

    init_db(tmp_path / "j2.duckdb")
    with db(tmp_path / "j2.duckdb") as con:
        journal_id = record_trade_journal_entry(con, ticker="amd", contract_id="AMD_C1")
        rows = query_rows(con, "SELECT * FROM trade_journal WHERE journal_id = ?", [journal_id])

    assert rows[0]["ticker"] == "AMD"  # normalized
    assert rows[0]["realized_status"] == "open"
