"""DuckDB schema and repository helpers."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import duckdb


from investment_panel.core.schema import SCHEMA_SQL


def connect(path: str | Path, read_only: bool = False, retries: int = 30, delay_seconds: float = 1.0) -> duckdb.DuckDBPyConnection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return duckdb.connect(str(db_path), read_only=read_only)
        except duckdb.IOException as exc:
            if "Could not set lock on file" not in str(exc) or attempt >= retries:
                raise
            last_error = exc
            time.sleep(delay_seconds)
    raise last_error or RuntimeError(f"Could not connect to DuckDB: {db_path}")


def init_db(path: str | Path) -> None:
    with connect(path) as con:
        con.sql(SCHEMA_SQL)
        _migrate_schema(con)


def _migrate_schema(con: duckdb.DuckDBPyConnection) -> None:
    columns = {row[1] for row in con.execute("PRAGMA table_info('portfolio_positions')").fetchall()}
    if "purchase_date" not in columns:
        con.execute("ALTER TABLE portfolio_positions ADD COLUMN purchase_date DATE")
    catalyst_columns = {row[1] for row in con.execute("PRAGMA table_info('catalysts')").fetchall()}
    for column, column_type in {
        "start_at": "TIMESTAMP",
        "end_at": "TIMESTAMP",
        "timezone": "TEXT",
        "event_scope": "TEXT",
        "event_kind": "TEXT",
        "importance": "TEXT",
        "verification_status": "TEXT",
        "source_url": "TEXT",
        "source_name": "TEXT",
    }.items():
        if column not in catalyst_columns:
            con.execute(f"ALTER TABLE catalysts ADD COLUMN {column} {column_type}")
    thesis_validation_columns = {row[1] for row in con.execute("PRAGMA table_info('agent_thesis_validation')").fetchall()}
    for column, column_type in {
        "strategy_version": "TEXT",
        "validation_date": "DATE",
        "candidate_event_id": "TEXT",
        "candidate_snapshot_time": "TIMESTAMP",
        "proof_status": "TEXT",
        "catalyst_status": "TEXT",
        "invalidation_status": "TEXT",
        "evidence_status": "TEXT",
        "red_team_status": "TEXT",
        "red_team_flags": "JSON",
    }.items():
        if column not in thesis_validation_columns:
            con.execute(f"ALTER TABLE agent_thesis_validation ADD COLUMN {column} {column_type}")
    for table, columns_to_add in {
        "discovered_universe": {
            "latest_observed_at": "TIMESTAMP",
            "next_event_at": "TIMESTAMP",
            "discovery_score": "DOUBLE",
        },
        "decision_queue": {
            "discovery_score": "DOUBLE",
            "decision_score": "DOUBLE",
            "action_score": "DOUBLE",
            "quote_freshness": "TEXT",
            "daily_analysis_freshness": "TEXT",
            "filing_freshness": "TEXT",
            "thesis_freshness": "TEXT",
            "overall_decision_freshness": "TEXT",
            "raw_source_rows": "INTEGER",
            "independent_source_count": "INTEGER",
            "evidence_items_count": "INTEGER",
            "primary_evidence_count": "INTEGER",
            "latest_observed_at": "TIMESTAMP",
            "next_event_at": "TIMESTAMP",
        },
        "symbol_decision_snapshots": {
            "quote_freshness": "TEXT",
            "daily_analysis_freshness": "TEXT",
            "filing_freshness": "TEXT",
            "thesis_freshness": "TEXT",
        },
        "source_registry": {
            "source_family": "TEXT",
            "raw_access": "TEXT",
        },
        "source_runs": {
            "item_count": "INTEGER",
            "ticker_count": "INTEGER",
            "failure_detail": "TEXT",
        },
        "source_items": {
            "source_run_id": "TEXT",
            "content_hash": "TEXT",
            "license_status": "TEXT",
        },
        "ticker_source_signals": {
            "needs_market_context": "BOOLEAN",
        },
        "manual_watchlist": {
            "watch_state": "TEXT",
        },
        "options_chain": {
            "rho": "DOUBLE",
            "theo": "DOUBLE",
            "bid_iv": "DOUBLE",
            "ask_iv": "DOUBLE",
            "contract_symbol": "TEXT",
        },
        "strategy_mutation_proposal": {
            "approved_by": "TEXT",
            "approved_at": "TIMESTAMP",
        },
        "candidate_event": {
            "quality_status": "TEXT",
            "quality_flags": "JSON",
        },
        "option_radar_opportunity": {
            "data_contract_status": "TEXT",
            "data_contract_failures": "JSON",
            "data_contract_satisfied": "JSON",
            "service_repair_jobs": "JSON",
            "service_repair_summary": "TEXT",
        },
        "radar_alert": {
            "created_at": "TIMESTAMP",
            "alert_type": "TEXT",
            "ticker": "TEXT",
            "contract_id": "TEXT",
            "event_id": "TEXT",
            "severity": "TEXT",
            "title": "TEXT",
            "detail": "TEXT",
            "acknowledged_at": "TIMESTAMP",
            "resolution_reason": "TEXT",
            "raw": "JSON",
        },
    }.items():
        existing_columns = {row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
        for column, column_type in columns_to_add.items():
            if column not in existing_columns:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
    con.execute("UPDATE manual_watchlist SET watch_state = 'watched' WHERE watch_state IS NULL OR watch_state = ''")


@contextmanager
def db(path: str | Path, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    con = connect(path, read_only=read_only)
    try:
        yield con
    finally:
        con.close()


def json_dumps(value: Any) -> str:
    def default(item: Any) -> Any:
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        return str(item)

    return json.dumps(value, ensure_ascii=False, default=default)


def upsert_instrument(con: duckdb.DuckDBPyConnection, instrument: dict[str, Any]) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO instruments
        (symbol, name, asset_class, sector, industry, category, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            instrument["symbol"].upper(),
            instrument.get("name"),
            instrument.get("asset_class"),
            instrument.get("sector"),
            instrument.get("industry"),
            instrument.get("category"),
            instrument.get("source"),
        ],
    )


def query_rows(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    result = con.execute(sql, params or [])
    columns = [column[0] for column in result.description]
    return [dict(zip(columns, row, strict=False)) for row in result.fetchall()]
