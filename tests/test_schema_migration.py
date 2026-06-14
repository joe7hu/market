"""schema.py is the single source of table shape; the migration derives from it.

These tests lock the property that an existing table created against an older
schema is brought forward to the DDL's columns, and that the DDL declares each
table exactly once (the radar_alert duplicate that previously hid columns).
"""

from __future__ import annotations

import re

import duckdb

from investment_panel.core.db import _ddl_table_columns, db, init_db
from investment_panel.core.schema import SCHEMA_SQL


def test_ddl_declares_each_table_once() -> None:
    names = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA_SQL)
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert duplicates == [], f"tables declared more than once in schema.py: {duplicates}"


def test_reconcile_brings_a_stale_table_forward(tmp_path) -> None:
    db_path = tmp_path / "stale.duckdb"
    # Simulate a database created against an older radar_alert shape.
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE radar_alert (alert_id TEXT PRIMARY KEY, created_at TIMESTAMP)")
    con.close()

    init_db(db_path)

    with db(db_path, read_only=True) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info('radar_alert')").fetchall()}
    expected = set(_ddl_table_columns()["radar_alert"])
    assert expected <= columns, f"reconcile left columns missing: {sorted(expected - columns)}"
    assert {"title", "detail", "resolution_reason"} <= columns


def test_fresh_db_has_all_declared_columns(tmp_path) -> None:
    db_path = tmp_path / "fresh.duckdb"
    init_db(db_path)
    with db(db_path, read_only=True) as con:
        for table, ddl_columns in _ddl_table_columns().items():
            existing = {row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
            missing = set(ddl_columns) - existing
            assert missing == set(), f"{table} missing declared columns: {sorted(missing)}"
