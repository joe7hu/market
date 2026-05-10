"""Status and snapshot helpers for NAS-backed source archive jobs."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import socket
from typing import Any

from investment_panel.core.config import AppConfig


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def write_source_status(config: AppConfig, status_id: str, payload: dict[str, Any]) -> Path:
    config.nas.status_dir.mkdir(parents=True, exist_ok=True)
    status_path = config.nas.status_dir / f"{status_id}.json"
    body = {
        "ok": True,
        "host": socket.gethostname(),
        "finishedAt": utc_now(),
        **payload,
    }
    status_path.write_text(json.dumps(body, indent=2, default=str) + "\n", encoding="utf-8")
    return status_path


def snapshot_duckdb(config: AppConfig, label: str = "market") -> Path | None:
    db_path = config.database.duckdb_path
    if not db_path.exists():
        return None
    config.nas.duckdb_snapshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    snapshot_path = config.nas.duckdb_snapshot_dir / f"{label}-{stamp}.duckdb"
    shutil.copy2(db_path, snapshot_path)
    return snapshot_path
