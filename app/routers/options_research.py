"""Bounded options-research HTTP routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query

from app.actions.options import OptionsActions
from app.data_access import config as config_owner
from app.options_history_contracts import DistributionShiftResponse, EventStudyResponse

router = APIRouter()


def _actions() -> OptionsActions:
    return OptionsActions(config_owner.load_config())


@router.get("/api/options/event-study", response_model=EventStudyResponse, response_model_exclude_unset=True)
def option_event_study(
    ticker: str = Query(..., min_length=1, max_length=16),
    event_kind: str = Query(..., min_length=1, max_length=64),
    as_of: datetime = Query(...),
) -> dict[str, Any]:
    return _actions().event_study(ticker=ticker, event_kind=event_kind, as_of=as_of)


@router.get("/api/options/history/distribution-shift", response_model=DistributionShiftResponse, response_model_exclude_unset=True)
def option_distribution_shift(
    symbol: str = Query("QQQ", min_length=1, max_length=16),
    as_of: datetime = Query(...),
) -> dict[str, Any]:
    return _actions().distribution_shift(symbol=symbol, as_of=as_of)
