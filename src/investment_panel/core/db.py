"""DuckDB schema and repository helpers."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import duckdb


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY,
    name TEXT,
    asset_class TEXT,
    sector TEXT,
    industry TEXT,
    category TEXT,
    source TEXT
);

CREATE TABLE IF NOT EXISTS prices_daily (
    symbol TEXT,
    date DATE,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume DOUBLE,
    source TEXT,
    PRIMARY KEY(symbol, date)
);

CREATE TABLE IF NOT EXISTS technical_features (
    symbol TEXT,
    date DATE,
    features JSON,
    PRIMARY KEY(symbol, date)
);

CREATE TABLE IF NOT EXISTS equity_fundamentals (
    symbol TEXT,
    period_end DATE,
    filing_date DATE,
    form_type TEXT,
    metrics JSON,
    source_url TEXT,
    PRIMARY KEY(symbol, period_end, form_type)
);

CREATE TABLE IF NOT EXISTS crypto_fundamentals (
    symbol TEXT,
    date DATE,
    metrics JSON,
    source TEXT,
    PRIMARY KEY(symbol, date)
);

CREATE TABLE IF NOT EXISTS disclosures (
    id TEXT PRIMARY KEY,
    source_type TEXT,
    trader_name TEXT,
    filer_name TEXT,
    symbol TEXT,
    event_date DATE,
    filed_date DATE,
    action TEXT,
    amount TEXT,
    raw JSON,
    source_url TEXT
);

CREATE TABLE IF NOT EXISTS birdclaw_theses (
    id TEXT PRIMARY KEY,
    symbol TEXT,
    author TEXT,
    created_at TIMESTAMP,
    thesis_summary TEXT,
    claims JSON,
    engagement JSON,
    source_url TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    run_date DATE,
    symbol TEXT,
    score DOUBLE,
    score_breakdown JSON,
    evidence JSON,
    decision TEXT
);

CREATE TABLE IF NOT EXISTS research_reports (
    id TEXT PRIMARY KEY,
    symbol TEXT,
    created_at TIMESTAMP,
    report_type TEXT,
    report_markdown TEXT,
    report_json JSON,
    evidence JSON
);

CREATE TABLE IF NOT EXISTS portfolio_positions (
    symbol TEXT PRIMARY KEY,
    quantity DOUBLE,
    avg_cost DOUBLE,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS theses (
    symbol TEXT PRIMARY KEY,
    thesis_json JSON,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalysts (
    id TEXT PRIMARY KEY,
    symbol TEXT,
    event_date DATE,
    event TEXT,
    expected_impact TEXT,
    source TEXT,
    raw JSON
);

CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY,
    checked_at TIMESTAMP,
    status TEXT,
    detail TEXT,
    source_url TEXT
);
"""


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
