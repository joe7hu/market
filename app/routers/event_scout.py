"""Event Scout API and point-in-time replay endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import deps
from app.actions.event_scout import persist_mrna_replay, process_signal
from investment_panel.core.event_scout import replay_mrna


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


@router.get("/api/decision-truth")
def decision_truth() -> dict[str, Any]:
    return deps._table_payload("decision_truth")


@router.get("/api/event-scout")
def event_scout_events(symbol: str | None = Query(None, min_length=1, max_length=16)) -> dict[str, Any]:
    config = deps.load_config()
    panel = deps.load_panel_data(
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


@router.get("/api/event-scout/packets")
def event_scout_packets(symbol: str | None = Query(None, min_length=1, max_length=16)) -> dict[str, Any]:
    config = deps.load_config()
    panel = deps.load_panel_data(
        config,
        table_names=("event_decision_packets",),
        query_symbol_filter={symbol.upper()} if symbol else None,
        query_row_limits={"event_decision_packets": 200},
    )
    rows = panel.rows("event_decision_packets")
    return {"status": {"ready": panel.status.ready, "message": panel.status.message, "source": panel.status.source}, "rows": rows, "count": len(rows)}


@router.get("/api/event-scout/replay")
def event_scout_replay(symbol: str = Query("MRNA", min_length=1, max_length=16)) -> dict[str, Any]:
    if symbol.strip().upper() != "MRNA":
        raise HTTPException(status_code=404, detail="Only the MRNA acceptance replay is available")
    return {"status": "replay_only", "packet": replay_mrna()}


@router.post("/api/event-scout/replay")
def persist_event_scout_replay(request: Request, symbol: str = Query("MRNA", min_length=1, max_length=16)) -> dict[str, Any]:
    deps._require_local_request(request)
    if symbol.strip().upper() != "MRNA":
        raise HTTPException(status_code=404, detail="Only the MRNA acceptance replay is available")
    config = deps.load_config()
    packet = persist_mrna_replay(config, symbol=symbol)
    return {"status": "persisted", "packet": packet}


@router.post("/api/event-scout/signals")
def event_scout_signal(payload: EventScoutSignalInput, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    config = deps.load_config()
    signal = payload.model_dump(mode="json")
    signal["payload"] = signal.pop("data")
    try:
        return process_signal(config, signal)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


__all__ = ["router"]
