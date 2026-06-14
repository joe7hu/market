"""Thesis, fundamentals, disclosures, and catalyst read-model routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app import deps

router = APIRouter()


@router.get("/api/theses")
def theses() -> dict[str, Any]:
    return deps._table_payload("theses")


@router.get("/api/thesis-monitor")
def thesis_monitor() -> dict[str, Any]:
    return deps._table_payload("thesis_monitor")


@router.get("/api/trader-twins")
def trader_twins() -> dict[str, Any]:
    return deps._table_payload("trader_twins")


@router.get("/api/catalysts")
def catalysts() -> dict[str, Any]:
    return deps._table_payload("catalysts")


@router.get("/api/fundamentals")
def fundamentals() -> dict[str, Any]:
    return deps._table_payload("fundamentals")


@router.get("/api/disclosures")
def disclosures() -> dict[str, Any]:
    return deps._table_payload("disclosures")
