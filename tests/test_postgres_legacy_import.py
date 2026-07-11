from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path

import duckdb
import psycopg

from investment_panel.database.legacy_import import LegacyImporter
from investment_panel.database.migrations import upgrade_database
from investment_panel.database.runtime import DatabaseRuntime


def _legacy_database(path: Path) -> None:
    with closing(duckdb.connect(str(path))) as connection:
        connection.execute("CREATE TABLE portfolio_positions (symbol TEXT PRIMARY KEY, quantity DOUBLE, avg_cost DOUBLE, notes TEXT, purchase_date DATE)")
        connection.execute("INSERT INTO portfolio_positions VALUES ('NVDA', 3, 125.5, 'core', DATE '2024-01-15')")
        connection.execute("CREATE TABLE manual_watchlist (symbol TEXT PRIMARY KEY, name TEXT, asset_class TEXT, notes TEXT, created_at TIMESTAMP, updated_at TIMESTAMP, watch_state TEXT)")
        connection.execute("INSERT INTO manual_watchlist VALUES ('BTC-USD', 'Bitcoin', 'crypto', 'macro', now(), now(), 'watched')")
        connection.execute("CREATE TABLE theses (symbol TEXT PRIMARY KEY, thesis_json JSON, updated_at TIMESTAMP)")
        connection.execute("INSERT INTO theses VALUES ('NVDA', ?, now())", [json.dumps({"core_thesis": "AI infrastructure leader", "invalidation": "below $95"})])
        connection.execute("CREATE TABLE trade_journal (journal_id TEXT PRIMARY KEY, created_at TIMESTAMP, strategy_version TEXT, ticker TEXT, contract_id TEXT, event_id TEXT, entry_premium DOUBLE, predicted_ev_multiple DOUBLE, predicted_p2x DOUBLE, conviction_score DOUBLE, opportunity_snapshot JSON, realized_return DOUBLE, realized_status TEXT, closed_at TIMESTAMP, notes TEXT, raw JSON)")
        connection.execute("INSERT INTO trade_journal VALUES ('journal-1', now(), 'v1', 'NVDA', 'contract-1', NULL, 5, 2, .3, 80, '{}', NULL, NULL, NULL, 'entry', '{}')")
        connection.execute("CREATE TABLE option_strategy_versions (strategy_version TEXT PRIMARY KEY, strategy_name TEXT, version INTEGER, created_at TIMESTAMP, status TEXT, parameters JSON, promoted_at TIMESTAMP, supersedes TEXT, notes TEXT)")
        connection.execute("INSERT INTO option_strategy_versions VALUES ('legacy-v1', 'legacy', 1, now(), 'shadow', '{\"max_spread_pct\": 0.25}', NULL, NULL, 'baseline')")
        connection.execute("CREATE TABLE agent_thesis (thesis_id TEXT PRIMARY KEY, ticker TEXT, created_at TIMESTAMP, core_thesis TEXT, raw JSON)")
        connection.execute("INSERT INTO agent_thesis VALUES ('thesis-agent-1', 'NVDA', now(), 'agent thesis', '{\"authority\": \"hypothesis_only\"}')")
        connection.execute("CREATE TABLE agent_postmortem (postmortem_id TEXT PRIMARY KEY, ticker TEXT, created_at TIMESTAMP, failure_type TEXT, raw JSON)")
        connection.execute("INSERT INTO agent_postmortem VALUES ('postmortem-1', 'NVDA', now(), 'late_entry', '{}')")
        connection.execute("CREATE TABLE strategy_mutation_proposal (proposal_id TEXT PRIMARY KEY, created_at TIMESTAMP, strategy_version TEXT, status TEXT, raw JSON)")
        connection.execute("INSERT INTO strategy_mutation_proposal VALUES ('proposal-1', now(), 'legacy-v1', 'rejected', '{}')")
        connection.execute("CREATE TABLE prices_daily (symbol TEXT, date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, source TEXT)")
        connection.execute("INSERT INTO prices_daily VALUES ('NVDA', DATE '2026-07-10', 160, 166, 159, 165, 1000, 'yahoo-chart')")
        connection.execute("CREATE TABLE source_items (id TEXT, source_id TEXT, source_kind TEXT, title TEXT, url TEXT, author TEXT, published_at TIMESTAMP, observed_at TIMESTAMP, summary TEXT, raw JSON, license_status TEXT)")
        connection.execute("INSERT INTO source_items VALUES ('news-1', 'news', 'news', 'NVDA update', 'https://example.test/news', 'Reporter', now(), now(), 'AI demand', '{}', 'provider_link_only')")
        connection.execute("CREATE TABLE disclosures (id TEXT, source_type TEXT, trader_name TEXT, filer_name TEXT, symbol TEXT, event_date DATE, filed_date DATE, action TEXT, amount TEXT, raw JSON, source_url TEXT)")
        connection.execute("INSERT INTO disclosures VALUES ('disc-1', 'public_disclosure_transaction', 'Trader', 'House', 'NVDA', DATE '2026-07-01', DATE '2026-07-03', 'BUY', '1001-15000', '{}', 'https://example.test/disc')")
        connection.execute("CREATE TABLE catalysts (id TEXT, symbol TEXT, event_date DATE, event TEXT, expected_impact TEXT, source TEXT, start_at TIMESTAMP, end_at TIMESTAMP, timezone TEXT, event_scope TEXT, event_kind TEXT, importance TEXT, verification_status TEXT, source_url TEXT, source_name TEXT, raw JSON)")
        connection.execute("INSERT INTO catalysts VALUES ('event-1', 'NVDA', DATE '2026-07-20', 'Earnings', 'Volatility', 'calendar', TIMESTAMP '2026-07-20 16:00:00', NULL, 'America/New_York', 'symbol', 'earnings', 'high', 'confirmed', 'https://example.test/event', 'Example', '{}')")
        connection.execute("CREATE TABLE option_snapshot (id INTEGER)")
        connection.execute("INSERT INTO option_snapshot SELECT * FROM range(100)")
        connection.execute("CREATE TABLE radar_alert (id INTEGER)")
        connection.execute("INSERT INTO radar_alert SELECT * FROM range(25)")


def test_selective_legacy_import_is_idempotent_and_reconciled(
    postgres_dsn: str,
    tmp_path: Path,
) -> None:
    upgrade_database(postgres_dsn)
    legacy_path = tmp_path / "legacy.duckdb"
    report_path = tmp_path / "reports" / "legacy-import.json"
    _legacy_database(legacy_path)
    before_hash = hashlib.sha256(legacy_path.read_bytes()).hexdigest()
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        importer = LegacyImporter(runtime, legacy_path)
        first = importer.run(report_path=report_path)
        second = importer.run()
    finally:
        runtime.close()

    assert first["status"] == "ok"
    assert first["source_counts"]["portfolio_positions"] == 1
    assert first["excluded_derived"] == {"option_snapshot": 100, "option_features": 0, "candidate_event": 0, "radar_alert": 25, "shadow_trade": 0}
    assert first["target_counts"] == {
        "portfolio_positions": 1,
        "manual_watchlist": 1,
        "theses_current": 1,
        "trade_journal": 1,
        "strategy_revisions": 1,
        "legacy_agent_tasks": 3,
        "price_bars": 1,
        "content_items": 1,
        "disclosures": 1,
        "market_events": 1,
    }
    assert second["imported_or_updated"]["theses"] == 0
    assert second["imported_or_updated"]["agent_thesis"] == 0
    assert second["target_counts"] == first["target_counts"]
    assert report_path.is_file()
    assert json.loads(report_path.read_text())["policy"]["duckdb_modified"] is False
    assert hashlib.sha256(legacy_path.read_bytes()).hexdigest() == before_hash

    with closing(psycopg.connect(postgres_dsn)) as connection:
        position = connection.execute(
            "SELECT i.symbol, p.quantity, p.average_cost FROM app.portfolio_position p JOIN catalog.instrument i ON i.id = p.instrument_id"
        ).fetchone()
        thesis_revisions = connection.execute("SELECT count(*) FROM app.thesis").fetchone()[0]
    assert position == ("NVDA", 3, 125.5)
    assert thesis_revisions == 1
