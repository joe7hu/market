"""DuckDB schema and repository helpers."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import duckdb


from investment_panel.core.schema import SCHEMA_SQL
from investment_panel.core.tradingview_identity import best_tradingview_symbol, primary_exchange


_CONNECT_LOCK = threading.RLock()


def connect(path: str | Path, read_only: bool = False, retries: int = 30, delay_seconds: float = 1.0) -> duckdb.DuckDBPyConnection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with _CONNECT_LOCK:
                return duckdb.connect(str(db_path), read_only=read_only)
        except duckdb.Error as exc:
            message = str(exc)
            # DuckDB caches one database instance per file per process: a second
            # connection must use the same configuration as the first. When this
            # process already holds a read-write connection (a refresh job or the
            # scheduler), a read-only open is rejected with this error. Read-write
            # is the only valid mode here, so fall back to it rather than failing
            # the read. (The reverse — downgrading a writer to read-only — is never
            # safe, so we only retry the read-only -> read-write direction.)
            if read_only and "different configuration than existing connections" in message:
                with _CONNECT_LOCK:
                    return duckdb.connect(str(db_path), read_only=False)
            if not (isinstance(exc, duckdb.IOException) and "Could not set lock on file" in message) or attempt >= retries:
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
    _backfill_instrument_market_identity(con)


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


def _backfill_instrument_market_identity(con: duckdb.DuckDBPyConnection) -> None:
    existing = {row[0] for row in con.execute("SELECT symbol FROM instrument_market_identity").fetchall()}
    rows = con.execute(
        """
        SELECT query, observed_at, symbol, description, instrument_type, exchange, country, currency, raw
        FROM tradingview_symbol_search
        ORDER BY query, observed_at DESC
        """
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for query, observed_at, symbol, description, instrument_type, exchange, country, currency, raw in rows:
        normalized = str(query or symbol or "").strip().upper()
        if not normalized or normalized in existing:
            continue
        group = grouped.setdefault(normalized, {"observed_at": observed_at, "rows": []})
        if observed_at != group["observed_at"]:
            continue
        group["rows"].append(
            {
                "symbol": symbol,
                "description": description,
                "instrument_type": instrument_type,
                "exchange": exchange,
                "country": country,
                "currency": currency,
                "raw": raw,
            }
        )
    for symbol, group in grouped.items():
        tradingview_symbol = best_tradingview_symbol(symbol, group["rows"])
        if not tradingview_symbol:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO instrument_market_identity
            (symbol, primary_exchange, tradingview_symbol, provider, observed_at, source, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                symbol,
                primary_exchange(tradingview_symbol),
                tradingview_symbol,
                "tradingview",
                group["observed_at"],
                "tradingview_symbol_search_backfill",
                json.dumps({"query": symbol, "rows": group["rows"]}, ensure_ascii=False, default=str),
            ],
        )


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
