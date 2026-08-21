"""Options radar, agent thesis/postmortem, learning-loop, and strategy routes."""
from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from app import panel_snapshot
from app.actions.options import OptionsActions
from app.contracts import OptionPaperEntryInput, StrategyPromotionInput
from app.data_access import config as config_owner
from app.data_access import loaders
from app.request_security import require_local_request
from app.response_contracts import (
    AgentSubmissionResponse,
    DecisionInboxResponse,
    OpportunityScorecardResponse,
    OptionHistoryHealthResponse,
    OptionHistorySymbolsResponse,
    OptionLearningCollectionResponse,
    OptionSignalDetailResponse,
    OptionTicketDetailResponse,
    PaperEntryResponse,
    RadarAlertAcknowledgementResponse,
    RecoveryEventResponse,
    RecoveryEventsResponse,
    RecoveryHealthResponse,
    StaticArbitrageVerificationResponse,
    StrategyPromotionResponse,
    OptionsWorkspaceResponse,
)
from app.options_history_contracts import (
    IVSurfaceGrid,
    IVCurveSet,
    OptionAnomalyPage,
    OptionChainPage,
    OptionSnapshotPage,
    OptionSurfaceEvidence,
    OptionSurfaceGroups,
    OptionsCandidatePage,
    OptionsDecisionBrief,
    OptionsLearningProgressPage,
    OptionsPaperJournalPage,
    RelativeValuePage,
)
from app.routers.options_research import router as research_router

router = APIRouter()
router.include_router(research_router)

RADAR_LEARNING_COLLECTIONS = frozenset({
    "candidate_event_mark",
    "candidate_event_attribution",
    "missed_winner_event",
    "strategy_mutation_proposal",
    "strategy_backtest_result",
    "strategy_forward_test_result",
    "strategy_cohort_result",
    "agent_thesis",
    "agent_thesis_request",
    "agent_thesis_validation",
    "agent_postmortem_request",
    "agent_postmortem",
})


def _decode_learning_cursor(
    cursor: str | None,
) -> tuple[datetime, tuple[str, str] | None]:
    if not cursor:
        return datetime.now(UTC), None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        snapshot_at = datetime.fromisoformat(payload["snapshot_at"])
        after_payload = payload.get("after")
        if snapshot_at.tzinfo is None:
            raise ValueError("cursor values are out of bounds")
        if not isinstance(after_payload, list) or len(after_payload) != 2:
            raise ValueError("cursor key is invalid")
        after = (str(after_payload[0]), str(after_payload[1]))
        return snapshot_at, after
    except (
        binascii.Error,
        KeyError,
        OverflowError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=400, detail="Invalid learning cursor") from exc


def _encode_learning_cursor(
    snapshot_at: datetime,
    after: tuple[Any, str],
) -> str:
    payload = json.dumps(
        {
            "snapshot_at": snapshot_at.isoformat(),
            "after": [str(after[0]), after[1]],
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _actions() -> OptionsActions:
    return OptionsActions(config_owner.load_config())


@router.get("/api/options/history/snapshots", response_model=OptionSnapshotPage, response_model_exclude_unset=True)
def historical_option_snapshots(
    symbol: str = Query("QQQ", min_length=1, max_length=16),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    include_partial: bool = False,
) -> dict[str, Any]:
    return _actions().history_snapshots(symbol=symbol, offset=offset, limit=limit, include_partial=include_partial)


@router.get("/api/options/history/symbols", response_model=OptionHistorySymbolsResponse, response_model_exclude_unset=True)
def historical_option_symbols() -> dict[str, Any]:
    return _actions().history_symbols()


@router.get("/api/options/history/chain", response_model=OptionChainPage, response_model_exclude_unset=True)
def historical_option_chain(
    symbol: str = Query("QQQ", min_length=1, max_length=16),
    snapshot: int | None = Query(None, ge=1),
    expiration: date | None = None,
    option_type: str | None = Query(None, pattern="^(call|put)$"),
    min_moneyness: float | None = Query(None, ge=-2, le=2),
    max_moneyness: float | None = Query(None, ge=-2, le=2),
    offset: int = Query(0, ge=0),
    limit: int = Query(250, ge=1, le=1000),
) -> dict[str, Any]:
    if min_moneyness is not None and max_moneyness is not None and min_moneyness > max_moneyness:
        raise HTTPException(status_code=422, detail="min_moneyness cannot exceed max_moneyness")
    return _actions().history_chain(
        symbol=symbol, snapshot=snapshot, expiration=expiration, option_type=option_type,
        min_moneyness=min_moneyness, max_moneyness=max_moneyness, offset=offset, limit=limit,
    )


@router.get("/api/options/history/surface", response_model=OptionSurfaceEvidence, response_model_exclude_unset=True)
def historical_option_surface(
    symbol: str = Query("QQQ", min_length=1, max_length=16),
    snapshot: int | None = Query(None, ge=1),
    expiration: date = Query(...),
    option_type: str = Query(..., pattern="^(call|put)$"),
) -> dict[str, Any]:
    return _actions().history_surface(symbol=symbol, snapshot=snapshot, expiration=expiration, option_type=option_type)


@router.get("/api/options/history/surface-groups", response_model=OptionSurfaceGroups, response_model_exclude_unset=True)
def historical_option_surface_groups(
    symbol: str = Query("QQQ", min_length=1, max_length=16),
    snapshot: int | None = Query(None, ge=1),
) -> dict[str, Any]:
    return _actions().history_surface_groups(symbol=symbol, snapshot=snapshot)


@router.get("/api/options/history/surface-grid", response_model=IVSurfaceGrid, response_model_exclude_unset=True)
def historical_option_surface_grid(
    symbol: str = Query("QQQ", min_length=1, max_length=16),
    snapshot: int | None = Query(None, ge=1),
    option_type: str = Query(..., pattern="^(call|put)$"),
    min_moneyness: float = Query(-0.30, ge=-2, le=2),
    max_moneyness: float = Query(0.30, ge=-2, le=2),
    max_dte: int = Query(365, ge=1, le=1095),
) -> dict[str, Any]:
    """Bounded provider-IV grid for the interactive surface explorer."""
    if min_moneyness >= max_moneyness:
        raise HTTPException(status_code=422, detail="min_moneyness must be less than max_moneyness")
    return _actions().history_surface_grid(
        symbol=symbol,
        snapshot=snapshot,
        option_type=option_type,
        min_moneyness=min_moneyness,
        max_moneyness=max_moneyness,
        max_dte=max_dte,
    )


@router.get("/api/options/history/curves", response_model=IVCurveSet, response_model_exclude_unset=True)
def historical_option_curves(
    symbol: str = Query("QQQ", min_length=1, max_length=16),
    snapshot: int | None = Query(None, ge=1),
    expiration: date | None = None,
) -> dict[str, Any]:
    return _actions().history_curves(symbol=symbol, snapshot=snapshot, expiration=expiration)


@router.get("/api/options/history/anomalies", response_model=OptionAnomalyPage, response_model_exclude_unset=True)
def historical_option_anomalies(
    symbol: str = Query("QQQ", min_length=1, max_length=16),
    snapshot: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
    limit: int = Query(250, ge=1, le=1000),
) -> dict[str, Any]:
    return _actions().history_anomalies(symbol=symbol, snapshot=snapshot, offset=offset, limit=limit)


@router.get("/api/options/history/health", response_model=OptionHistoryHealthResponse, response_model_exclude_unset=True)
def historical_option_health(symbol: str | None = Query(None, min_length=1, max_length=16)) -> dict[str, Any]:
    """Operational storage and completeness reporting for the Health surface."""
    return _actions().history_health(symbol=symbol)


@router.get("/api/health/options-recovery", response_model=RecoveryHealthResponse, response_model_exclude_unset=True)
def recovery_option_health() -> dict[str, Any]:
    """Recovery capacity, lease, storage, and scheduler diagnostics for Health only."""
    return _actions().recovery_health()


@router.get("/api/options/events", response_model=RecoveryEventsResponse, response_model_exclude_unset=True)
def recovery_events(
    status: Literal["active", "deferred_capacity", "closed", "invalidated"] | None = None,
    cohort: str | None = Query(None, min_length=1, max_length=96),
    include_invalidated: bool = Query(False),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=250),
) -> dict[str, Any]:
    return _actions().recovery_events(
        status=status, cohort=cohort, include_invalidated=include_invalidated,
        offset=offset, limit=limit,
    )


@router.get("/api/options/events/{event_id}", response_model=RecoveryEventResponse, response_model_exclude_unset=True)
def recovery_event(
    event_id: UUID,
    cohort: str | None = Query(None, min_length=1, max_length=96),
    include_invalidated: bool = Query(False),
) -> dict[str, Any]:
    detail = _actions().recovery_event(
        str(event_id),
        cohort=cohort,
        include_invalidated=include_invalidated,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Options recovery event not found")
    return detail


@router.get("/api/options/decision-brief", response_model=OptionsDecisionBrief, response_model_exclude_unset=True)
def options_decision_brief(
    symbol: str = Query("QQQ", min_length=1, max_length=16),
    lane: Literal["thesis", "anomaly"] = "thesis",
) -> dict[str, Any]:
    return _actions().decision_brief(symbol=symbol, lane=lane)


@router.get("/api/options/workspace", response_model=OptionsWorkspaceResponse, response_model_exclude_unset=True)
def options_workspace(
    symbol: str = Query("QQQ", min_length=1, max_length=16),
    lane: Literal["thesis", "anomaly"] = "thesis",
) -> dict[str, Any]:
    return _actions().workspace(symbol=symbol, lane=lane)


@router.get("/api/options/candidates", response_model=OptionsCandidatePage, response_model_exclude_unset=True)
def options_candidates(
    symbol: str = Query("QQQ", min_length=1, max_length=16),
    scope: Literal["current", "history"] = "current",
    lane: Literal["thesis", "anomaly"] | None = None,
    paper_state: Literal["COLLECTING", "WATCH", "PAPER_READY", "REJECT"] | None = None,
    structure: Literal["long_call", "long_put", "call_debit_spread", "put_debit_spread"] | None = None,
    expiration: date | None = None,
    cursor: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
) -> dict[str, Any]:
    if cursor is not None:
        try:
            offset = int(cursor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="cursor must be an integer offset") from exc
    return _actions().candidates(
        symbol=symbol, scope=scope, lane=lane, paper_state=paper_state, structure=structure,
        expiration=expiration, offset=offset, limit=limit,
    )


@router.get("/api/options/history/relative-values", response_model=RelativeValuePage, response_model_exclude_unset=True)
def historical_relative_values(
    symbol: str = Query("QQQ", min_length=1, max_length=16),
    snapshot: int | None = Query(None, ge=1),
    classification: Literal["relative_cheap", "relative_rich", "historical_static_arbitrage_candidate", "verified_static_arbitrage_candidate", "rejected"] | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
) -> dict[str, Any]:
    return _actions().relative_values(symbol=symbol, snapshot=snapshot, classification=classification, offset=offset, limit=limit)


@router.post("/api/options/history/static-arbitrage-candidates/{candidate_id}/verify", response_model=StaticArbitrageVerificationResponse, response_model_exclude_unset=True)
def verify_static_arbitrage_candidate(candidate_id: int, request: Request) -> dict[str, Any]:
    require_local_request(request)
    try:
        return _actions().verify_static_arbitrage(candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/options/paper-journal", response_model=OptionsPaperJournalPage, response_model_exclude_unset=True)
def options_paper_journal(
    symbol: str = Query("QQQ", min_length=1, max_length=16),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
) -> dict[str, Any]:
    return _actions().paper_journal(symbol=symbol, offset=offset, limit=limit)


@router.get("/api/options/shadow-observations", response_model=OptionsPaperJournalPage, response_model_exclude_unset=True)
def options_shadow_observations(
    symbol: str = Query("QQQ", min_length=1, max_length=16),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    include_legacy: bool = False,
) -> dict[str, Any]:
    return _actions().shadow_observations(
        symbol=symbol,
        offset=offset,
        limit=limit,
        include_legacy=include_legacy,
    )


@router.get("/api/options/learning-progress", response_model=OptionsLearningProgressPage, response_model_exclude_unset=True)
def options_learning_progress(
    symbol: str = Query("QQQ", min_length=1, max_length=16),
) -> dict[str, Any]:
    return _actions().learning_progress(symbol=symbol)


@router.get("/api/opportunity-scorecard", response_model=OpportunityScorecardResponse, response_model_exclude_unset=True)
def opportunity_scorecard(
    lane: Literal["radar", "qqq", "recovery"] = Query("radar"),
    window: int = Query(120, ge=1, le=3650),
) -> dict[str, Any]:
    return _actions().opportunity_scorecard(
        lane=lane,
        window_days=window,
    )


@router.get("/api/decision-inbox", response_model=DecisionInboxResponse, response_model_exclude_unset=True)
def decision_inbox(
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None, max_length=256),
) -> dict[str, Any]:
    try:
        return _actions().decision_inbox(limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/options-radar/signals/{decision_id}", response_model=OptionSignalDetailResponse, response_model_exclude_unset=True)
def option_radar_signal_detail(decision_id: UUID) -> dict[str, Any]:
    detail = _actions().signal_detail(decision_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Options-radar signal not found")
    return detail


@router.get("/api/options-radar/learning/{collection}", response_model=OptionLearningCollectionResponse, response_model_exclude_unset=True)
def option_radar_learning_collection(
    collection: str,
    cursor: str | None = Query(None, max_length=256),
    limit: int = Query(25, ge=1, le=100),
) -> dict[str, Any]:
    if collection not in RADAR_LEARNING_COLLECTIONS:
        raise HTTPException(status_code=404, detail="Unknown options-radar learning collection")
    snapshot_at, after = _decode_learning_cursor(cursor)
    try:
        page, count, next_after = loaders.load_table_panel_page(
            config_owner.load_config(),
            collection,
            limit=limit,
            snapshot_at=snapshot_at,
            after=after,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    next_cursor = _encode_learning_cursor(snapshot_at, next_after) if next_after else None
    return {
        "collection": collection,
        "items": page,
        "count": count,
        "next_cursor": next_cursor,
    }


@router.get("/api/options/tickets/{decision_id}", response_model=OptionTicketDetailResponse, response_model_exclude_unset=True)
def option_trade_ticket(decision_id: UUID) -> dict[str, Any]:
    detail = _actions().signal_detail(decision_id)
    if detail is not None:
        ticket = detail.get("ticket")
        if isinstance(ticket, dict):
            return _ticket_detail_contract(ticket, detail)
    # A recovery signal has an analysis.decision row for provenance, so the
    # legacy detail reader can find it without owning its v4 ticket.  Always
    # prefer the canonical recovery ticket before treating the decision as an
    # incomplete legacy publication.
    recovery_ticket = _actions().recovery_ticket(decision_id)
    if recovery_ticket is not None:
        return _ticket_detail_contract(recovery_ticket, detail or {})
    if detail is None:
        raise HTTPException(status_code=404, detail="Option decision not found")
    raise HTTPException(status_code=409, detail="Current publication has no trade ticket; refresh the decision surface")


def _ticket_detail_contract(ticket: dict[str, Any], signal: dict[str, Any]) -> dict[str, Any]:
    """Keep legacy ticket fields while adding the immutable deep-dive contract.

    Older clients read ``ticket_version`` and ``legs`` at the top level.  New
    clients use the named sections so a signal remains inspectable even when a
    ticker dossier has no independent data.
    """

    outcome_fields = (
        "maturity_state", "observed_through", "current_return", "return_1d",
        "return_5d", "return_20d", "return_60d", "peak_return", "max_drawdown",
        "paper_status", "credit_captured", "collateral_return", "assigned_basis",
        "strike_touched",
    )
    publication = {
        "id": signal.get("publication_id") or (ticket.get("publication_lineage") or {}).get("publication_id"),
        "scope": signal.get("publication_scope") or (ticket.get("publication_lineage") or {}).get("publication_scope"),
        "published_at": signal.get("published_at") or (ticket.get("publication_lineage") or {}).get("published_at"),
        "current": bool(signal.get("current_publication", False)),
    }
    return {
        # Top-level ticket fields preserve the former endpoint contract.
        **ticket,
        "ticket": ticket,
        "signal": signal,
        "publication": publication,
        "evidence": list(signal.get("evidence") or []),
        "outcome": {name: signal.get(name) for name in outcome_fields if signal.get(name) is not None},
        "agent_provenance": (ticket.get("provenance") or {}).get("thesis") or {},
    }


@router.post("/api/options-radar/signals/{decision_id}/paper-entry", response_model=PaperEntryResponse, response_model_exclude_unset=True)
def stage_option_radar_paper_entry(
    decision_id: UUID,
    payload: OptionPaperEntryInput,
    request: Request,
) -> dict[str, Any]:
    require_local_request(request)
    try:
        result = _actions().stage_paper_entry(decision_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


@router.post("/api/agent-thesis", response_model=AgentSubmissionResponse, response_model_exclude_unset=True)
def submit_agent_thesis(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    require_local_request(request)
    try:
        result = _actions().submit_thesis(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


@router.post("/api/agent-postmortems", response_model=AgentSubmissionResponse, response_model_exclude_unset=True)
def submit_agent_postmortem(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    require_local_request(request)
    try:
        result = _actions().submit_postmortem(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


@router.post("/api/radar-alerts/{alert_id}/ack", response_model=RadarAlertAcknowledgementResponse, response_model_exclude_unset=True)
def acknowledge_radar_alert_endpoint(alert_id: str, request: Request) -> dict[str, Any]:
    require_local_request(request)
    acknowledged = _actions().acknowledge_alert(alert_id)
    panel_snapshot.invalidate_context_cache()
    if not acknowledged:
        raise HTTPException(status_code=404, detail="Radar alert not found")
    return acknowledged


@router.post("/api/strategy-mutation-proposals/{proposal_id}/promote", response_model=StrategyPromotionResponse, response_model_exclude_unset=True)
def promote_strategy_mutation_endpoint(
    proposal_id: str,
    request: Request,
    payload: StrategyPromotionInput | None = None,
) -> dict[str, Any]:
    require_local_request(request)
    approved_by = payload.approved_by.strip() if payload else "joe"
    try:
        result = _actions().promote_strategy(proposal_id, approved_by=approved_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result
