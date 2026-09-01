"""Ticker dossier routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app import panel_snapshot
from app import dependencies
from app.actions.tickers import TickerActions
from app.contracts import TickerPaperEntryInput
from app.data_access import loaders, payloads
from app.response_contracts import TickerDecisionSnapshotResponse, TickerDetailResponse, TickerPaperEntryResponse
from investment_panel.core.config import AppConfig
from investment_panel.core.decision import TickerDecision

router = APIRouter()


# The ticker page needs decision conclusions and compact authority identifiers,
# not the immutable evidence bodies.  The complete validated artifact remains
# available from ``/decision-snapshot`` for audit and paper-entry checks.
_TICKER_DETAIL_EXCLUDE = {
    "opportunity_episode": True,
    "learning_history": True,
    "instrument_state_snapshot": True,
    "alpha_signals": True,
    "opportunity_rank": True,
    "trade_plan": True,
    "outcome_attributions": True,
    "expressions": True,
    "data_requests": True,
    "learning": True,
}


@router.get(
    "/api/tickers/{ticker}",
    response_model=TickerDetailResponse,
    response_model_exclude=_TICKER_DETAIL_EXCLUDE,
    response_model_exclude_unset=True,
)
def ticker_detail(
    ticker: str,
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    normalized = ticker.strip().upper()
    _, panel_data = panel_snapshot.context(
        cache_key=f"ticker:{normalized}",
        loader=lambda config: loaders.load_ticker_panel_data(config, normalized),
        config_loader=lambda: config,
    )
    return payloads.ticker_payload(panel_data, normalized)


@router.get("/api/tickers/{ticker}/decision-snapshot", response_model=TickerDecisionSnapshotResponse, response_model_exclude_unset=True)
def ticker_decision_snapshot(
    ticker: str,
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    normalized = ticker.strip().upper()
    _, panel_data = panel_snapshot.context(
        cache_key=f"ticker:{normalized}",
        loader=lambda active_config: loaders.load_ticker_panel_data(active_config, normalized),
        config_loader=lambda: config,
    )
    return payloads.ticker_payload(panel_data, normalized)["ticker_decision"]


@router.post(
    "/api/tickers/{ticker}/paper-entry",
    response_model=TickerPaperEntryResponse,
    response_model_exclude_unset=True,
)
def ticker_paper_entry(
    ticker: str,
    payload: TickerPaperEntryInput,
    config: AppConfig = Depends(dependencies.get_config),
    actions: TickerActions = Depends(dependencies.get_ticker_actions),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    normalized = ticker.strip().upper()
    _, panel_data = panel_snapshot.context(
        cache_key=f"ticker:{normalized}",
        loader=lambda active_config: loaders.load_ticker_panel_data(active_config, normalized),
        config_loader=lambda: config,
    )
    decision_payload = payloads.ticker_payload(panel_data, normalized)["ticker_decision"]
    decision = TickerDecision.model_validate(decision_payload)
    try:
        result = actions.stage_paper_entry(
            ticker=normalized,
            decision=decision,
            payload=payload.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


__all__ = ["router"]
