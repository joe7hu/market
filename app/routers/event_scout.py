"""Event Scout API and point-in-time replay endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.actions.event_scout import persist_mrna_replay, process_signal
from app import dependencies
from app.data_access import loaders
from app.response_contracts import (
    EventScoutEventsResponse,
    EventScoutPacketsResponse,
    EventScoutReplayResponse,
    EventScoutSignalResponse,
)
from investment_panel.core.event_scout import replay_mrna
from investment_panel.core.config import AppConfig


router = APIRouter()


class EventScoutSignalInput(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    trigger_type: str = Field(min_length=1, max_length=64)
    event_kind: str = Field(default="material_event", min_length=1, max_length=96)
    observed_at: datetime | None = None
    source_url: str | None = None
    source_kind: str = "unknown"
    headline: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


@router.get("/api/event-scout", response_model=EventScoutEventsResponse, response_model_exclude_unset=True)
def event_scout_events(
    symbol: str | None = Query(None, min_length=1, max_length=16),
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    panel = loaders.load_panel_data(
        config,
        table_names=("event_scout_events", "decision_truth"),
        query_symbol_filter={symbol.upper()} if symbol else None,
        query_row_limits={"event_scout_events": 200, "decision_truth": 500},
    )
    return {
        "status": {"ready": panel.status.ready, "message": panel.status.message, "source": panel.status.source},
        "tables": {
            "event_scout_events": {"rows": panel.rows("event_scout_events"), "count": len(panel.rows("event_scout_events"))},
            "decision_truth": {"rows": panel.rows("decision_truth"), "count": len(panel.rows("decision_truth"))},
        },
    }


@router.get("/api/event-scout/packets", response_model=EventScoutPacketsResponse, response_model_exclude_unset=True)
def event_scout_packets(
    symbol: str | None = Query(None, min_length=1, max_length=16),
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    panel = loaders.load_panel_data(
        config,
        table_names=("event_decision_packets",),
        query_symbol_filter={symbol.upper()} if symbol else None,
        query_row_limits={"event_decision_packets": 200},
    )
    rows = panel.rows("event_decision_packets")
    return {"status": {"ready": panel.status.ready, "message": panel.status.message, "source": panel.status.source}, "rows": rows, "count": len(rows)}


@router.get("/api/event-scout/replay", response_model=EventScoutReplayResponse, response_model_exclude_unset=True)
def event_scout_replay(symbol: str = Query("MRNA", min_length=1, max_length=16)) -> dict[str, Any]:
    if symbol.strip().upper() != "MRNA":
        raise HTTPException(status_code=404, detail="Only the MRNA acceptance replay is available")
    return {"status": "replay_only", "packet": replay_mrna()}


@router.post("/api/event-scout/replay", response_model=EventScoutReplayResponse, response_model_exclude_unset=True)
def persist_event_scout_replay(
    symbol: str = Query("MRNA", min_length=1, max_length=16),
    config: AppConfig = Depends(dependencies.get_config),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    if symbol.strip().upper() != "MRNA":
        raise HTTPException(status_code=404, detail="Only the MRNA acceptance replay is available")
    packet = persist_mrna_replay(config, symbol=symbol)
    return {"status": "persisted", "packet": packet}


@router.post("/api/event-scout/signals", response_model=EventScoutSignalResponse, response_model_exclude_unset=True)
def event_scout_signal(
    payload: EventScoutSignalInput,
    config: AppConfig = Depends(dependencies.get_config),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    signal = payload.model_dump(mode="json")
    signal["payload"] = signal.pop("data")
    try:
        return process_signal(config, signal)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


__all__ = ["router"]
