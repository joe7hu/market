from __future__ import annotations

from pathlib import Path

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.event_calendar import (
    MarketEvent,
    delete_legacy_requested_week_events,
    geopolitical_event_from_report,
    parse_bls_schedule_rows,
    parse_fed_calendar_text,
    upsert_events,
)
from investment_panel.core.panel import load_panel_data
from investment_panel.jobs.update_event_calendar import run as run_event_calendar


def test_event_parsers_extract_primary_source_rows() -> None:
    bls_rows = parse_bls_schedule_rows(
        """
        Reference Month Release Date Release Time
        April 2026 May 12, 2026 08:30 AM
        May 2026 Jun. 10, 2026 08:30 AM
        """
    )
    assert bls_rows[0].event_date == "2026-05-12"
    assert bls_rows[0].start_at == "2026-05-12T08:30:00"
    assert bls_rows[0].verification_status == "confirmed"

    fed_rows = parse_fed_calendar_text(
        """
        4:30 p.m.
        H.4.1 - Factors Affecting Reserve Balances
        7, 14, 21, 28
        """
    )
    assert [row.event_date for row in fed_rows] == ["2026-05-07", "2026-05-14", "2026-05-21", "2026-05-28"]

    summit = geopolitical_event_from_report(
        "2026-05-14",
        "2026-05-15",
        "Trump-Xi Beijing summit",
        "https://example.com/summit",
        "Example",
    )
    assert summit.end_at == "2026-05-15T23:59:00"
    assert summit.event_kind == "geopolitical"


def test_requested_week_events_round_trip_through_calendar_read_model(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        count = upsert_events(
            con,
            [
                MarketEvent(
                    id="test-cpi",
                    event_date="2026-05-12",
                    event="April 2026 CPI report",
                    expected_impact="Inflation print can reprice risk.",
                    source="bls",
                    verification_status="confirmed",
                    source_url="https://www.bls.gov/schedule/news_release/cpi.htm?lv=true",
                ),
                MarketEvent(
                    id="test-senate",
                    event_date="2026-05-11",
                    event="Senate cloture vote on Kevin Warsh Fed nomination",
                    expected_impact="Fed nomination process event.",
                    source="senate_schedule",
                    verification_status="tentative",
                    source_url="https://www.senate.gov/",
                ),
                MarketEvent(
                    id="test-fed",
                    event_date="2026-05-14",
                    event="Fed Governor Barr balance sheet speech",
                    expected_impact="Balance sheet remarks.",
                    source="federal_reserve",
                    verification_status="confirmed",
                    source_url="https://www.federalreserve.gov/newsevents/calendar.htm",
                ),
            ],
        )
        con.execute(
            """
            INSERT OR REPLACE INTO earnings_events (symbol, event_date, event_type, metrics, source)
            VALUES ('NVDA', '2026-05-14', 'earnings', '{}', 'test')
            """
        )
        rows = query_rows(con, "SELECT event, verification_status, source_url FROM catalysts ORDER BY event_date")

    assert count == 3
    assert any(row["verification_status"] == "tentative" for row in rows)
    assert all("source_url" in row for row in rows)

    panel = load_panel_data({"database": {"duckdb_path": str(db_path)}})
    events = panel["tables"]["catalysts"]
    labels = [row["event"] for row in events]
    assert "April 2026 CPI report" in labels
    assert "Senate cloture vote on Kevin Warsh Fed nomination" in labels
    assert "Fed Governor Barr balance sheet speech" in labels
    assert "Fed/FOMC chair speech watch item" not in labels
    assert "earnings" in labels
    assert any(row["source_url"] for row in events if row["event"] == "April 2026 CPI report")


def test_event_calendar_job_writes_status(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
database:
  duckdb_path: {tmp_path / "investment.duckdb"}
nas:
  status_dir: {tmp_path / "status"}
event_sources:
  enabled: false
  seed_requested_week: false
""",
        encoding="utf-8",
    )

    result = run_event_calendar(str(config_path))

    assert result["status"] == "disabled"
    assert result["events"] == 0
    assert Path(result["status_path"]).exists()
    config = load_config(config_path)
    with db(config.database.duckdb_path, read_only=True) as con:
        health = query_rows(con, "SELECT * FROM source_health WHERE source = 'event_calendar'")
    assert health[0]["status"] == "disabled"


def test_event_calendar_cleanup_removes_legacy_hardcoded_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        upsert_events(
            con,
            [
                MarketEvent(
                    id="macro-2026-05-12-bls-cpi-april",
                    event_date="2026-05-12",
                    event="Legacy hardcoded CPI",
                    expected_impact="legacy",
                    source="bls",
                )
            ],
        )
        deleted = delete_legacy_requested_week_events(con)
        rows = query_rows(con, "SELECT * FROM catalysts")

    assert deleted >= 0
    assert rows == []
