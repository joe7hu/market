"""Source catalog, audit, and superinvestor detail routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.actions.sources import SourceActions
from app.actions.superinvestors import SuperinvestorActions
from app.data_access import config as config_owner
from app.response_contracts import (
    SourceAuditResponse,
    SourceCatalogResponse,
    SourceDetailResponse,
    SuperinvestorDetailResponse,
)

router = APIRouter()


def _actions() -> SourceActions:
    return SourceActions(config_owner.load_config())


def _superinvestor_actions() -> SuperinvestorActions:
    return SuperinvestorActions(config_owner.load_config())


@router.get("/api/sources/{source_id}", response_model=SourceDetailResponse, response_model_exclude_unset=True)
def source_detail(source_id: str) -> dict[str, Any]:
    return _actions().detail(source_id)


@router.get("/api/source-catalog", response_model=SourceCatalogResponse, response_model_exclude_unset=True)
def source_catalog() -> dict[str, Any]:
    """Authoritative data-source catalog joined with live freshness/health status."""
    return _actions().catalog()


@router.get("/api/source-ingestion-audit", response_model=SourceAuditResponse, response_model_exclude_unset=True)
def source_audit() -> dict[str, Any]:
    return _actions().audit()


@router.get("/api/superinvestors/{investor_key}", response_model=SuperinvestorDetailResponse, response_model_exclude_unset=True)
def superinvestor_detail(investor_key: str) -> dict[str, Any]:
    row = _superinvestor_actions().detail(investor_key)
    if row is None:
        raise HTTPException(status_code=404, detail="Superinvestor portfolio not found")
    return row


__all__ = ["router"]
