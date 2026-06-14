"""DuckDB schema and repository helpers."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from functools import lru_cache
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
    # schema.py is the single source of table shape; bring existing tables up to
    # it, then apply data backfills that are not column additions.
    _reconcile_columns_to_ddl(con)
    con.execute("UPDATE manual_watchlist SET watch_state = 'watched' WHERE watch_state IS NULL OR watch_state = ''")


@lru_cache(maxsize=1)
def _ddl_table_columns() -> dict[str, dict[str, str]]:
    """``table -> {column: type}`` for every table the DDL declares.

    DuckDB parses its own DDL in a throwaway in-memory database, so the column
    set is derived from schema.py rather than restated as a hand-maintained
    migration list that can drift. Cached because ``SCHEMA_SQL`` is constant.
    """
    reference = duckdb.connect(":memory:")
    try:
        reference.execute(SCHEMA_SQL)
        rows = reference.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
        ).fetchall()
    finally:
        reference.close()
    columns: dict[str, dict[str, str]] = {}
    for table_name, column_name, data_type in rows:
        columns.setdefault(table_name, {})[column_name] = data_type
    return columns


def _reconcile_columns_to_ddl(con: duckdb.DuckDBPyConnection) -> None:
    """Add any column the DDL declares that an existing table is missing.

    ``CREATE TABLE IF NOT EXISTS`` leaves a pre-existing table untouched, so a DB
    created against an older schema keeps its old column set. This walks each
    declared table forward to schema.py's columns without dropping or retyping
    anything an existing column already holds.
    """
    for table, ddl_columns in _ddl_table_columns().items():
        existing = {row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
        if not existing:
            continue
        for name, col_type in ddl_columns.items():
            if name not in existing:
                con.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" {col_type}')


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
