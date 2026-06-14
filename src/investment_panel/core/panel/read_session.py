"""DuckDB session selection for panel reads."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from investment_panel.core.db import _ddl_table_columns, db, init_db


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

    if _schema_needs_migration(db_path):
        init_db(db_path)

    with db(db_path, read_only=True) as con:
        yield con


def _schema_needs_migration(db_path: Path) -> bool:
    try:
        with db(db_path, read_only=True) as con:
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
