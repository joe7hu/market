"""Source catalog, audit, and superinvestor detail routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app import dependencies
from app.actions.sources import SourceActions
from app.actions.superinvestors import SuperinvestorActions
from app.response_contracts import (
    SourceAuditResponse,
    SourceCatalogResponse,
    SourceDetailResponse,
    SuperinvestorDetailResponse,
)

router = APIRouter()


@router.get("/api/sources/{source_id}", response_model=SourceDetailResponse, response_model_exclude_unset=True)
def source_detail(
    source_id: str,
    actions: SourceActions = Depends(dependencies.get_source_actions),
) -> dict[str, Any]:
    return actions.detail(source_id)


@router.get("/api/source-catalog", response_model=SourceCatalogResponse, response_model_exclude_unset=True)
def source_catalog(actions: SourceActions = Depends(dependencies.get_source_actions)) -> dict[str, Any]:
    """Authoritative data-source catalog joined with live freshness/health status."""
    return actions.catalog()


@router.get("/api/source-ingestion-audit", response_model=SourceAuditResponse, response_model_exclude_unset=True)
def source_audit(actions: SourceActions = Depends(dependencies.get_source_actions)) -> dict[str, Any]:
    return actions.audit()


@router.get("/api/superinvestors/{investor_key}", response_model=SuperinvestorDetailResponse, response_model_exclude_unset=True)
def superinvestor_detail(
    investor_key: str,
    actions: SuperinvestorActions = Depends(dependencies.get_superinvestor_actions),
) -> dict[str, Any]:
    row = actions.detail(investor_key)
    if row is None:
        raise HTTPException(status_code=404, detail="Superinvestor portfolio not found")
    return row


__all__ = ["router"]
