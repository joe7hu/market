"""Operational storage observability.  No archive mutations are exposed over HTTP."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app import deps

router = APIRouter()


@router.get("/api/health/storage")
def storage_health() -> dict[str, Any]:
    config = deps.load_config()
    try:
        return deps.storage_health(config)
    except Exception as exc:
        # Health stays observable during a mount or migration outage.
        return {"available": False, "message": f"{type(exc).__name__}: {exc}"}
