"""Read-only paper-book and learning-workbench routes."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from investment_panel.api import dependencies
from investment_panel.api.data_access import continuous_advisor as continuous_owner
from investment_panel.api.response_contracts import (
    LearningOverview,
    PaperBookPerformance,
    PaperTradeDetail,
    PaperTradePage,
)
from investment_panel.settings import AppConfig


router = APIRouter()


@router.get(
    "/api/paper/performance",
    response_model=PaperBookPerformance,
    response_model_exclude_unset=True,
)
def paper_performance(
    symbol: str | None = Query(default=None, min_length=1, max_length=16),
    strategy_revision: int | None = Query(default=None, ge=1),
    lifecycle: str | None = Query(default=None, pattern="^(staged|open|closed)$"),
    repository: dependencies.PaperWorkbenchRepository = Depends(
        dependencies.get_paper_workbench
    ),
) -> dict[str, Any]:
    return repository.performance(
        symbol=symbol, strategy_revision=strategy_revision, lifecycle=lifecycle
    )


@router.get(
    "/api/paper/trades",
    response_model=PaperTradePage,
    response_model_exclude_unset=True,
)
def paper_trades(
    symbol: str | None = Query(default=None, min_length=1, max_length=16),
    strategy_revision: int | None = Query(default=None, ge=1),
    lifecycle: str | None = Query(default=None, pattern="^(staged|open|closed)$"),
    limit: int = Query(default=100, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1024),
    repository: dependencies.PaperWorkbenchRepository = Depends(
        dependencies.get_paper_workbench
    ),
) -> dict[str, Any]:
    try:
        decoded = _decode_cursor(cursor)
        payload = repository.trades(
            symbol=symbol,
            strategy_revision=strategy_revision,
            lifecycle=lifecycle,
            limit=limit,
            cursor=decoded,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = payload.pop("rows")
    next_cursor = (
        _encode_cursor(rows[-1]["staged_at"], rows[-1]["paper_order_id"])
        if len(rows) == limit
        else None
    )
    return {**payload, "rows": rows, "next_cursor": next_cursor}


@router.get(
    "/api/paper/trades/{trade_id}",
    response_model=PaperTradeDetail,
    response_model_exclude_unset=True,
)
def paper_trade_detail(
    trade_id: str,
    repository: dependencies.PaperWorkbenchRepository = Depends(
        dependencies.get_paper_workbench
    ),
) -> dict[str, Any]:
    try:
        normalized_id = str(UUID(trade_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid paper trade identifier"
        ) from exc
    detail = repository.trade(normalized_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Paper trade not found")
    return detail


@router.get(
    "/api/research/overview",
    response_model=LearningOverview,
    response_model_exclude_unset=True,
)
def research_overview(
    config: AppConfig = Depends(dependencies.get_config),
    repository: dependencies.PaperWorkbenchRepository = Depends(
        dependencies.get_paper_workbench
    ),
) -> dict[str, Any]:
    performance = repository.performance()
    advisor = continuous_owner.overview(config)
    enabled = bool(config.agents.thesis_monitor.continuous_enabled)
    prompt_health = advisor.get("strategy_health") or {}
    challenger = prompt_health.get("challenger") or {}
    return {
        "paper_only": True,
        "as_of": performance["as_of"],
        "source_watermark": performance["source_watermark"],
        "paper": performance,
        "strategy_lane": {
            "status": "collecting_outcomes"
            if performance["counts"]["filled_orders"]
            else "no_paper_fills",
            "deployed_version": None,
            "challenger": None,
            "permitted_automatic_action": "human_review_required",
            "evidence_counts": performance["counts"],
            "blockers": performance["missing_evidence_reasons"],
        },
        "prediction_lane": {
            "status": "disabled" if not enabled else "advisory_only",
            "deployed_version": prompt_health.get("active_prompt_version"),
            "challenger": challenger.get("version"),
            "permitted_automatic_action": "advisory_only",
            "overview": advisor,
            "blockers": [],
        },
        "events": [],
        "actions": [],
    }


def _decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        at = datetime.fromisoformat(str(payload["staged_at"]))
        trade_id = str(payload["paper_order_id"])
    except (
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("Invalid paper trade cursor") from exc
    if at.tzinfo is None or not trade_id:
        raise ValueError("Invalid paper trade cursor")
    return at, trade_id


def _encode_cursor(staged_at: datetime, paper_order_id: str) -> str:
    raw = json.dumps(
        {"staged_at": staged_at.isoformat(), "paper_order_id": paper_order_id},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


__all__ = ["router"]
