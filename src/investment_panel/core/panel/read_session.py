"""DuckDB session selection for panel reads."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from typing import Any, Iterator

from investment_panel.core.db import _ddl_table_columns, db, init_db

_TRUTHY_OFF = {"0", "false", "off", "no"}


@contextmanager
def panel_read_session(db_path: Path, *, needs_write: bool) -> Iterator[Any | None]:
    """Open the correct DuckDB session for a panel read.

    Write-capable reads own schema initialization. Pure reads open read-only
    unless a schema probe shows the database needs migration first.
    """

    if needs_write:
        init_db(db_path)
        with db(db_path, read_only=False) as con:
            yield con
        return

    if not db_path.exists():
        yield None
        return

    read_only = _pure_reads_should_be_read_only()
    read_retries = _panel_read_lock_retries()
    if _schema_needs_migration(db_path, read_only=read_only, retries=read_retries):
        init_db(db_path)

    with db(db_path, read_only=read_only, retries=read_retries, delay_seconds=0.1) as con:
        yield con


def _pure_reads_should_be_read_only() -> bool:
    """Keep panel reads from queuing behind refresh writers."""

    override = os.environ.get("MARKET_PANEL_READ_ONLY", "1").strip().lower()
    return override not in _TRUTHY_OFF


def _panel_read_lock_retries() -> int:
    raw = os.environ.get("MARKET_PANEL_READ_LOCK_RETRIES", "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _schema_needs_migration(db_path: Path, *, read_only: bool = True, retries: int = 0) -> bool:
    try:
        with db(db_path, read_only=read_only, retries=retries, delay_seconds=0.1) as con:
            rows = con.execute(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'main'"
            ).fetchall()
    except Exception as exc:
        if "Could not set lock on file" in str(exc):
            return False
        return True

    existing: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        existing.setdefault(str(table_name), set()).add(str(column_name))
    for table_name, ddl_columns in _ddl_table_columns().items():
        if table_name not in existing:
            return True
        if set(ddl_columns) - existing[table_name]:
            return True
    return False
