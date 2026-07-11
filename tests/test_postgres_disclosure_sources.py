from __future__ import annotations

from pathlib import Path
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
