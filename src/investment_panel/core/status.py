"""Status and snapshot helpers for NAS-backed source archive jobs."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
from pathlib import Path
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
    # Derive ok from the payload's own status instead of hardcoding True, so a job
    # that failed (e.g. offline gateway, hard error) cannot publish a green status.
    job_status = str(payload.get("status", "ok")).lower()
    body = {
        "ok": job_status not in {"error", "gateway_offline", "missing_dependency", "failed"},
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
