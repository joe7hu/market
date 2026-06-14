"""Followed-source coverage and ingestion-audit routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app import deps

router = APIRouter()


@router.get("/api/source-health")
def source_health() -> dict[str, Any]:
    return deps._table_payload("source_health")


@router.get("/api/sources")
def sources() -> dict[str, Any]:
    return deps._table_payload("sources")


@router.get("/api/sources/{source_id}")
def source_detail(source_id: str) -> dict[str, Any]:
    config = deps.load_config()
    with deps._CONTEXT_LOCK:
        with deps.db(deps.database_path(config), read_only=True) as con:
            return deps.source_detail_payload(con, source_id, ensure_sources=False)


@router.get("/api/source-items")
def source_items() -> dict[str, Any]:
    return deps._table_payload("source_items")


@router.get("/api/source-ticker-rankings")
def source_ticker_rankings() -> dict[str, Any]:
    return deps._table_payload("source_ticker_rankings")


@router.get("/api/source-runs")
def source_runs() -> dict[str, Any]:
    return deps._table_payload("source_runs")


@router.get("/api/ticker-source-signals")
def ticker_source_signals() -> dict[str, Any]:
    return deps._table_payload("ticker_source_signals")


@router.get("/api/source-ingestion-audit")
def source_audit() -> dict[str, Any]:
    config = deps.load_config()
    with deps._CONTEXT_LOCK:
        with deps.db(deps.database_path(config), read_only=True) as con:
            return deps.source_ingestion_audit(con, sync_sources=False)


@router.get("/api/source-consensus")
def source_consensus() -> dict[str, Any]:
    return deps._table_payload("source_consensus")


@router.get("/api/ownership-consensus")
def ownership_consensus() -> dict[str, Any]:
    return deps._table_payload("ownership_consensus")
