from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from investment_panel.core import sec
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.disclosures import (
    backfill_trader_disclosure_history,
    extract_13f_trackers,
    extract_public_disclosure_csvs,
    extract_tracked_traders,
    ingest_public_disclosure_csvs,
    ingest_13f_trackers,
    parse_information_table_xml,
    recent_13f_filings,
    rebuild_trader_replica_portfolios,
)
from investment_panel.jobs.update_disclosures import run as run_update_disclosures


SUBMISSIONS_PAYLOAD = {
    "cik": "0001067983",
    "name": "BERKSHIRE HATHAWAY INC",
    "filings": {
        "recent": {
            "accessionNumber": ["0000950123-26-000111", "0000950123-26-000110", "0000950123-26-000109"],
            "form": ["13F-HR", "4", "13F-HR/A"],
            "filingDate": ["2026-02-14", "2026-01-02", "2025-11-14"],
            "reportDate": ["2025-12-31", "2026-01-02", "2025-09-30"],
            "acceptanceDateTime": ["2026-02-14T12:00:00.000Z", "2026-01-02T12:00:00.000Z", "2025-11-14T12:00:00.000Z"],
            "primaryDocument": ["primary_doc.xml", "xslF345X05/doc4.xml", "primary_doc.xml"],
            "primaryDocDescription": ["13F-HR", "FORM 4", "13F-HR/A"],
        }
    },
}


INFO_TABLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>123456</value>
    <shrsOrPrnAmt>
      <sshPrnamt>915000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority>
      <Sole>915000</Sole>
      <Shared>0</Shared>
      <None>0</None>
    </votingAuthority>
  </infoTable>
  <infoTable>
    <nameOfIssuer>BANK AMER CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>060505104</cusip>
    <value>2345</value>
    <shrsOrPrnAmt>
      <sshPrnamt>10000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority>
      <Sole>10000</Sole>
      <Shared>0</Shared>
      <None>0</None>
    </votingAuthority>
  </infoTable>
</informationTable>
"""


def test_recent_13f_filings_filters_submission_forms() -> None:
    filings = recent_13f_filings(SUBMISSIONS_PAYLOAD, limit=5)

    assert [filing["form"] for filing in filings] == ["13F-HR", "13F-HR/A"]
    assert filings[0]["accession_number"] == "0000950123-26-000111"
    assert filings[0]["report_date"] == "2025-12-31"
    assert filings[0]["filer_name"] == "BERKSHIRE HATHAWAY INC"


def test_parse_information_table_xml_keeps_cusip_not_symbol() -> None:
    holdings = parse_information_table_xml(INFO_TABLE_XML)

    assert holdings[0]["name"] == "APPLE INC"
    assert holdings[0]["cusip"] == "037833100"
    assert holdings[0]["value_thousands"] == 123456
    assert "symbol" not in holdings[0]


def test_ingest_13f_trackers_stores_metadata_and_holdings(monkeypatch: Any, tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)

    monkeypatch.setattr(sec, "company_submissions", lambda cik, user_agent: SUBMISSIONS_PAYLOAD)
    monkeypatch.setattr(
        sec,
        "filing_directory_index",
        lambda cik, accession_number, user_agent: {
            "directory": {
                "item": [
                    {"name": "primary_doc.xml"},
                    {"name": "FilingSummary.xml"},
                    {"name": "form13fInfoTable.xml"},
                ]
            }
        },
    )
    monkeypatch.setattr(sec, "filing_document_text", lambda cik, accession_number, filename, user_agent: INFO_TABLE_XML)

    with db(db_path) as con:
        result = ingest_13f_trackers(
            con,
            [{"cik": "1067983", "name": "Berkshire tracker"}],
            "test-agent",
            default_max_filings=1,
            fetch_holdings=True,
        )

    assert result["trackers_checked"] == 1
    assert result["filings_ingested"] == 1
    assert result["holdings_ingested"] == 2
    with db(db_path, read_only=True) as con:
        rows = query_rows(con, "SELECT source_type, trader_name, filer_name, symbol, event_date, filed_date, action, raw FROM disclosures")
    assert rows[0]["source_type"] == "13f"
    assert rows[0]["trader_name"] == "Berkshire tracker"
    assert rows[0]["filer_name"] == "BERKSHIRE HATHAWAY INC"
    assert rows[0]["symbol"] is None
    assert str(rows[0]["event_date"]) == "2025-12-31"
    assert str(rows[0]["filed_date"]) == "2026-02-14"
    assert rows[0]["action"] == "13F-HR"
    raw = json.loads(rows[0]["raw"])
    assert "delayed quarterly disclosure" in raw["lag_caveat"]
    assert raw["ticker_mapping_caveat"].startswith("No ticker symbols")
    assert raw["holdings_parse_status"] == "parsed"
    assert raw["holdings"][0]["cusip"] == "037833100"
    assert "symbol" not in raw["holdings"][0]


def test_update_disclosures_reads_configured_13f_trackers(monkeypatch: Any, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
database:
  duckdb_path: {tmp_path / "investment.duckdb"}
nas:
  source_root: {tmp_path / "nas"}
  status_dir: {tmp_path / "nas" / "status"}
  market_dir: {tmp_path / "nas" / "market-mini"}
  duckdb_snapshot_dir: {tmp_path / "nas" / "market-mini" / "duckdb-snapshots"}
market_data:
  user_agent: test-agent
disclosures:
  13f_trackers:
    - name: Berkshire tracker
      cik: 1067983
      max_filings: 1
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(sec, "company_submissions", lambda cik, user_agent: SUBMISSIONS_PAYLOAD)

    result = run_update_disclosures(str(config_path), fetch_holdings=False)

    assert result["status"] == "disclosures_updated"
    assert result["trackers_configured"] == 1
    assert result["filings_ingested"] == 1
    with db(tmp_path / "investment.duckdb", read_only=True) as con:
        rows = query_rows(con, "SELECT source_type, raw FROM disclosures")
    assert rows[0]["source_type"] == "13f"
    raw = json.loads(rows[0]["raw"])
    assert raw["holdings_parse_status"] == "not_requested"


def test_extract_13f_trackers_accepts_aliases() -> None:
    trackers = extract_13f_trackers({"disclosures": {"thirteen_f_trackers": [{"name": "Fund", "cik": "123"}]}})

    assert trackers == [{"cik": "123", "name": "Fund", "max_filings": None}]


def test_public_disclosure_csv_builds_replica_portfolio(tmp_path: Path) -> None:
    csv_path = tmp_path / "nancy.csv"
    csv_path.write_text(
        """trader_name,symbol,transaction_type,transaction_date,filed_date,amount_min,amount_max,source_url
Nancy Pelosi,NVDA,BUY,2025-01-15,2025-02-10,1000000,5000000,https://disclosures-clerk.house.gov/
Nancy Pelosi,NVDA,SELL,2025-04-15,2025-05-10,250000,500000,https://disclosures-clerk.house.gov/
Nancy Pelosi,AAPL,BUY,2025-02-01,2025-02-20,500000,1000000,https://disclosures-clerk.house.gov/
""",
        encoding="utf-8",
    )
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute("INSERT INTO prices_daily VALUES ('NVDA', '2025-01-15', 90, 100, 80, 100, 1, 'test')")
        con.execute("INSERT INTO prices_daily VALUES ('NVDA', '2025-04-15', 110, 120, 100, 120, 1, 'test')")
        con.execute("INSERT INTO prices_daily VALUES ('NVDA', '2026-01-01', 180, 200, 170, 200, 1, 'test')")
        con.execute("INSERT INTO prices_daily VALUES ('AAPL', '2025-02-01', 180, 200, 170, 200, 1, 'test')")
        con.execute("INSERT INTO prices_daily VALUES ('AAPL', '2026-01-01', 225, 250, 220, 250, 1, 'test')")
        result = ingest_public_disclosure_csvs(
            con,
            [{"path": str(csv_path), "trader_name": "Nancy Pelosi", "filer_name": "House periodic transaction report"}],
        )
        replica = rebuild_trader_replica_portfolios(con)

    assert result["public_disclosure_rows_ingested"] == 3
    assert replica["trader_replica_portfolios_built"] == 1
    with db(db_path, read_only=True) as con:
        rows = query_rows(con, "SELECT source_type, trader_name, action, raw FROM disclosures ORDER BY source_type DESC")
    model = next(row for row in rows if row["source_type"] == "trader_portfolio_model")
    raw = json.loads(model["raw"])
    assert model["trader_name"] == "Nancy Pelosi"
    assert model["action"] == "PORTFOLIO_MODEL"
    assert raw["holdings"][0]["symbol"] == "NVDA"
    assert raw["transactions_count"] == 3
    assert raw["source_caveat"].startswith("Replica portfolios")


def test_extract_public_disclosure_csvs_from_config(tmp_path: Path) -> None:
    sources = extract_public_disclosure_csvs(
        {"disclosures": {"public_disclosure_csvs": [{"path": "nancy.csv", "trader_name": "Nancy Pelosi"}]}},
        tmp_path,
    )

    assert sources == [
        {
            "path": str(tmp_path / "nancy.csv"),
            "trader_name": "Nancy Pelosi",
            "source_type": "public_disclosure_transaction",
            "filer_name": "Nancy Pelosi",
            "source_kind": "public_disclosure",
        }
    ]


def test_tracked_trader_backfill_replaces_history_and_rebuilds_model(tmp_path: Path) -> None:
    historical = tmp_path / "historical.csv"
    daily = tmp_path / "daily.csv"
    historical.write_text(
        """symbol,transaction_type,transaction_date,filed_date,amount_min,amount_max,source_url
NVDA,BUY,2024-01-02,2024-02-01,100000,200000,https://source/historical
""",
        encoding="utf-8",
    )
    daily.write_text(
        """symbol,transaction_type,transaction_date,filed_date,amount_min,amount_max,source_url
AAPL,BUY,2024-02-01,2024-02-15,50000,100000,https://source/daily
""",
        encoding="utf-8",
    )
    traders = extract_tracked_traders(
        {
            "disclosures": {
                "tracked_traders": [
                    {
                        "name": "Nancy Pelosi",
                        "filer_name": "House PTR",
                        "source_kind": "house_ptr",
                        "historical_csvs": [{"path": "historical.csv"}],
                        "daily_csvs": [{"path": "daily.csv"}],
                    }
                ]
            }
        },
        tmp_path,
    )

    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute("INSERT INTO prices_daily VALUES ('NVDA', '2024-01-02', 90, 100, 80, 100, 1, 'test')")
        con.execute("INSERT INTO prices_daily VALUES ('NVDA', '2026-01-01', 180, 200, 170, 200, 1, 'test')")
        con.execute("INSERT INTO prices_daily VALUES ('AAPL', '2024-02-01', 180, 200, 170, 200, 1, 'test')")
        con.execute("INSERT INTO prices_daily VALUES ('AAPL', '2026-01-01', 225, 250, 220, 250, 1, 'test')")
        result = backfill_trader_disclosure_history(con, traders[0])

    assert result["historical_files_configured"] == 1
    assert result["daily_files_configured"] == 1
    assert result["public_disclosure_rows_ingested"] == 2
    assert result["trader_replica_portfolios_built"] == 1
    with db(db_path, read_only=True) as con:
        rows = query_rows(con, "SELECT source_type, trader_name, raw FROM disclosures ORDER BY source_type")
    assert [row["source_type"] for row in rows].count("public_disclosure_transaction") == 2
    model = next(row for row in rows if row["source_type"] == "trader_portfolio_model")
    raw = json.loads(model["raw"])
    assert model["trader_name"] == "Nancy Pelosi"
    assert {holding["symbol"] for holding in raw["holdings"]} == {"NVDA", "AAPL"}
