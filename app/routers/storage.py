"""Operational storage observability.  No archive mutations are exposed over HTTP."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.data_access import config as config_owner
from app.actions.storage import storage_health as storage_health_owner

router = APIRouter()


@router.get("/api/health/storage")
def storage_health() -> dict[str, Any]:
    config = config_owner.load_config()
    try:
        return storage_health_owner(config)
    except Exception as exc:
        # Health stays observable during a mount or migration outage.
        return {"available": False, "message": f"{type(exc).__name__}: {exc}"}
