"""Status and snapshot helpers for NAS-backed source archive jobs."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import shutil
import socket
from typing import Any

from investment_panel.core.config import AppConfig

logger = logging.getLogger("market.status")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def write_source_status(config: AppConfig, status_id: str, payload: dict[str, Any]) -> Path | None:
    """Write a NAS-backed job status file.

    The status archive lives on a NAS mount that is only present on the production
    host. On a dev machine (or any host where the mount is missing) the directory
    cannot be created — that must not abort the job, whose real work (DB writes)
    has already completed. In that case we log and return None.
    """

    status_path = config.nas.status_dir / f"{status_id}.json"
    body = {
        "ok": True,
        "host": socket.gethostname(),
        "finishedAt": utc_now(),
        **payload,
    }
    try:
        config.nas.status_dir.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps(body, indent=2, default=str) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("skipped source status %s: NAS unavailable (%s)", status_id, exc)
        return None
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
