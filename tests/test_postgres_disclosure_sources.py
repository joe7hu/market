from __future__ import annotations

from pathlib import Path
import json
from types import SimpleNamespace

from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.jobs import update_disclosure_sources


def test_disclosure_csv_is_archived_normalized_and_idempotent(
    migrated_postgres_dsn: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    csv_path = tmp_path / "pelosi.csv"
    # Keep the amount simple enough for CSV parsing while still proving the raw
    # source file, not a JSON copy, is the archived payload.
    csv_path.write_text(
        "id,symbol,transaction_date,transaction_type,amount,source_url\n"
        "tx-1,NVDA,2026-07-01,PURCHASE,1001-15000,https://example.test/tx-1\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "disclosures:\n"
        "  public_disclosure_csvs:\n"
        f"    - path: {csv_path}\n"
        "      trader_name: Nancy Pelosi\n"
        "      filer_name: House disclosures\n",
        encoding="utf-8",
    )
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    config = SimpleNamespace(database=SimpleNamespace(url=migrated_postgres_dsn))
    monkeypatch.setattr(update_disclosure_sources, "load_config", lambda _path=None: config)
    monkeypatch.setattr(update_disclosure_sources, "runtime_for_config", lambda _config: runtime)
    try:
        assert update_disclosure_sources.run(str(config_path))["rows_ingested"] == 1
        assert update_disclosure_sources.run(str(config_path))["rows_ingested"] == 1
        with runtime.read() as connection:
            row = connection.execute(
                """
                SELECT disclosure.source_key, instrument.symbol, disclosure.trader_name,
                       disclosure.action, disclosure.amount_text, payload.archive_uri
                FROM raw.disclosure disclosure
                JOIN catalog.instrument instrument ON instrument.id = disclosure.instrument_id
                JOIN ingest.payload payload ON payload.id = disclosure.payload_id
                """
            ).fetchone()
            count = connection.execute("SELECT count(*) AS count FROM raw.disclosure").fetchone()["count"]
        assert count == 1
        assert row["source_key"] == "tx-1"
        assert row["symbol"] == "NVDA"
        assert row["trader_name"] == "Nancy Pelosi"
        assert row["action"] == "PURCHASE"
        assert row["amount_text"] == "1001-15000"
        assert str(row["archive_uri"]).endswith("pelosi.csv")
    finally:
        runtime.close()


def test_house_refresh_archives_pdf_and_skips_existing_document(
    migrated_postgres_dsn: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "disclosures:\n"
        "  tracked_traders:\n"
        "    - name: Nancy Pelosi\n"
        "      official_house:\n"
        "        last_name: Pelosi\n"
        "        state: CA\n"
        "        start_year: 2025\n"
        "        end_year: 2026\n",
        encoding="utf-8",
    )
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    config = SimpleNamespace(
        database=SimpleNamespace(url=migrated_postgres_dsn),
        market_data=SimpleNamespace(user_agent="test-agent"),
        nas=SimpleNamespace(market_dir=tmp_path / "nas"),
        report_dir=tmp_path / "reports",
    )
    filing = {
        "document_id": "20030001", "url": "https://house.example/20030001.pdf",
        "filing_type": "PTR Original", "name": "Nancy Pelosi",
    }
    parsed = {
        "id": "house:20030001:0", "symbol": "NVDA", "transaction_date": "2026-06-01",
        "filed_date": "2026-06-15", "transaction_type": "BUY", "amount": "1001-15000",
        "filer_name": "Nancy Pelosi",
    }
    fetches = []
    monkeypatch.setattr(update_disclosure_sources, "load_config", lambda _path=None: config)
    monkeypatch.setattr(update_disclosure_sources, "runtime_for_config", lambda _config: runtime)
    monkeypatch.setattr(update_disclosure_sources, "search_house_member_filings", lambda *_args, **_kwargs: [filing])
    monkeypatch.setattr(
        update_disclosure_sources,
        "fetch_house_pdf_bytes",
        lambda *_args: fetches.append("pdf") or b"original-pdf",
    )
    monkeypatch.setattr(update_disclosure_sources, "parse_house_pdf_bytes", lambda _payload: "parsed")
    monkeypatch.setattr(update_disclosure_sources, "parse_house_disclosure_text", lambda *_args: [parsed])
    try:
        first = update_disclosure_sources.run(str(config_path))
        second = update_disclosure_sources.run(str(config_path))
        assert first["rows_ingested"] == 1
        assert second["rows_ingested"] == 0
        assert fetches == ["pdf"]
        with runtime.read() as connection:
            row = connection.execute(
                """
                SELECT disclosure.details->>'source_document_id' AS document_id,
                       payload.archive_uri
                FROM raw.disclosure disclosure
                JOIN ingest.payload payload ON payload.id = disclosure.payload_id
                """
            ).fetchone()
        assert row["document_id"] == "20030001"
        assert str(row["archive_uri"]).endswith("20030001.pdf")
    finally:
        runtime.close()


def test_13f_refresh_archives_sec_payloads_and_skips_existing_accession(
    migrated_postgres_dsn: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "disclosures:\n"
        "  13f_trackers:\n"
        "    - name: Test Fund\n"
        "      cik: '0000000123'\n"
        "      max_filings: 1\n"
        "      ticker_map:\n"
        "        '67066G104': NVDA\n",
        encoding="utf-8",
    )
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    config = SimpleNamespace(
        database=SimpleNamespace(url=migrated_postgres_dsn),
        market_data=SimpleNamespace(user_agent="test-agent"),
        nas=SimpleNamespace(market_dir=tmp_path / "nas"),
        report_dir=tmp_path / "reports",
    )
    submissions = {
        "name": "Test Fund",
        "filings": {"recent": {
            "form": ["13F-HR"], "accessionNumber": ["0000000123-26-000001"],
            "filingDate": ["2026-05-15"], "reportDate": ["2026-03-31"],
            "primaryDocument": ["primary.xml"],
        }},
    }
    index = {"directory": {"item": [{"name": "primary.xml"}, {"name": "infotable.xml"}]}}
    info = b"""<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
      <infoTable><nameOfIssuer>NVIDIA</nameOfIssuer><titleOfClass>COM</titleOfClass>
      <cusip>67066G104</cusip><value>25000</value><shrsOrPrnAmt><sshPrnamt>100</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
    </informationTable>"""
    fetches = []

    def fake_get(url: str, _agent: str) -> bytes:
        fetches.append(url)
        if "submissions" in url:
            return json.dumps(submissions).encode()
        if url.endswith("index.json"):
            return json.dumps(index).encode()
        return info

    monkeypatch.setattr(update_disclosure_sources, "load_config", lambda _path=None: config)
    monkeypatch.setattr(update_disclosure_sources, "runtime_for_config", lambda _config: runtime)
    monkeypatch.setattr(update_disclosure_sources, "_http_bytes", fake_get)
    try:
        first = update_disclosure_sources.run(str(config_path))
        second = update_disclosure_sources.run(str(config_path))
        assert first["rows_ingested"] == 1
        assert second["rows_ingested"] == 0
        assert sum(url.endswith("infotable.xml") for url in fetches) == 1
        with runtime.read() as connection:
            row = connection.execute(
                "SELECT source_key, details FROM raw.disclosure WHERE source_type = '13f'"
            ).fetchone()
        assert row["source_key"] == "0000000123-26-000001"
        assert row["details"]["holdings"][0]["symbol"] == "NVDA"
        assert row["details"]["holdings_value_thousands"] == 25000
    finally:
        runtime.close()
