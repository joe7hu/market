"""Operational storage observability.  No archive mutations are exposed over HTTP."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app import dependencies
from app.actions.storage import storage_health as storage_health_owner
from app.response_contracts import StorageHealthResponse
from investment_panel.core.config import AppConfig

router = APIRouter()


@router.get("/api/health/storage", response_model=StorageHealthResponse, response_model_exclude_unset=True)
def storage_health(config: AppConfig = Depends(dependencies.get_config)) -> dict[str, Any]:
    try:
        return storage_health_owner(config)
    except Exception as exc:
        # Health stays observable during a mount or migration outage.
        return {"available": False, "message": f"{type(exc).__name__}: {exc}"}
