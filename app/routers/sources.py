"""Followed-source coverage and ingestion-audit routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app import deps
from app.actions.sources import SourceActions

router = APIRouter()


def _actions() -> SourceActions:
    return SourceActions(deps.load_config())


@router.get("/api/source-health")
def source_health() -> dict[str, Any]:
    return deps._table_payload("source_health")


@router.get("/api/sources")
def sources() -> dict[str, Any]:
    return deps._table_payload("sources")


@router.get("/api/sources/{source_id}")
def source_detail(source_id: str) -> dict[str, Any]:
    return _actions().detail(source_id)


@router.get("/api/source-items")
def source_items() -> dict[str, Any]:
    return deps._table_payload("source_items")


@router.get("/api/source-ticker-rankings")
def source_ticker_rankings() -> dict[str, Any]:
    return deps._table_payload("source_ticker_rankings")


@router.get("/api/source-runs")
def source_runs() -> dict[str, Any]:
    return deps._table_payload("source_runs")


@router.get("/api/source-catalog")
def source_catalog() -> dict[str, Any]:
    """Authoritative data-source catalog joined with live freshness/health status."""
    return _actions().catalog()


@router.get("/api/ticker-source-signals")
def ticker_source_signals() -> dict[str, Any]:
    return deps._table_payload("ticker_source_signals")


@router.get("/api/source-ingestion-audit")
def source_audit() -> dict[str, Any]:
    return _actions().audit()


@router.get("/api/source-consensus")
def source_consensus() -> dict[str, Any]:
    return deps._table_payload("source_consensus")


@router.get("/api/ownership-consensus")
def ownership_consensus() -> dict[str, Any]:
    return deps._table_payload("ownership_consensus")
