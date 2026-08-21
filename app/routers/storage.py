"""Operational storage observability.  No archive mutations are exposed over HTTP."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app import dependencies
from app.response_contracts import StorageHealthResponse

router = APIRouter()


@router.get("/api/health/storage", response_model=StorageHealthResponse, response_model_exclude_unset=True)
def storage_health(service: Any = Depends(dependencies.get_storage_archive_service)) -> dict[str, Any]:
    try:
        return service.health()
    except Exception as exc:
        # Health stays observable during a mount or migration outage.
        return {"available": False, "message": f"{type(exc).__name__}: {exc}"}
