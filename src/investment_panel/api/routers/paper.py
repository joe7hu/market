"""Read-only paper-book and learning-workbench routes."""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
from datetime import date, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

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
    book: str = Query(default="paper", pattern="^paper$"),
    sleeve: str | None = Query(default=None, min_length=1, max_length=64),
    symbol: str | None = Query(default=None, min_length=1, max_length=16),
    instrument_kind: str | None = Query(default=None, min_length=1, max_length=32),
    strategy_revision: int | None = Query(default=None, ge=1),
    lifecycle: str | None = Query(default=None, pattern="^(staged|open|closed)$"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    lane: str | None = Query(default=None, min_length=1, max_length=32),
    structure: str | None = Query(default=None, min_length=1, max_length=64),
    evidence_class: str | None = Query(default=None, pattern="^(verified|partial|unavailable)$"),
    reconciliation_status: str | None = Query(default=None, pattern="^(verified|partial|unavailable)$"),
    repository: dependencies.PaperWorkbenchRepository = Depends(
        dependencies.get_paper_workbench
    ),
) -> dict[str, Any]:
    try:
        payload = repository.performance(
            book=book,
            sleeve=sleeve,
            symbol=symbol,
            instrument_kind=instrument_kind,
            strategy_revision=strategy_revision,
            lifecycle=lifecycle,
            date_from=date_from,
            date_to=date_to,
            lane=lane,
            structure=structure,
            evidence_class=evidence_class,
            reconciliation_status=reconciliation_status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = payload.get("trades") or []
    scope_id = (payload.get("scope") or {}).get("scope_id")
    payload["next_cursor"] = (
        _encode_cursor(
            rows[-1]["staged_at"],
            rows[-1]["paper_order_id"],
            snapshot_at=payload.get("as_of"),
            snapshot_id=payload.get("snapshot_id"),
            scope_id=scope_id,
        )
        if rows and payload.get("counts", {}).get("total_orders", 0) > len(rows)
        else None
    )
    return payload


@router.get(
    "/api/paper/trades",
    response_model=PaperTradePage,
    response_model_exclude_unset=True,
)
def paper_trades(
    book: str = Query(default="paper", pattern="^paper$"),
    sleeve: str | None = Query(default=None, min_length=1, max_length=64),
    symbol: str | None = Query(default=None, min_length=1, max_length=16),
    instrument_kind: str | None = Query(default=None, min_length=1, max_length=32),
    strategy_revision: int | None = Query(default=None, ge=1),
    lifecycle: str | None = Query(default=None, pattern="^(staged|open|closed)$"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    lane: str | None = Query(default=None, min_length=1, max_length=32),
    structure: str | None = Query(default=None, min_length=1, max_length=64),
    evidence_class: str | None = Query(default=None, pattern="^(verified|partial|unavailable)$"),
    reconciliation_status: str | None = Query(default=None, pattern="^(verified|partial|unavailable)$"),
    limit: int = Query(default=100, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1024),
    repository: dependencies.PaperWorkbenchRepository = Depends(
        dependencies.get_paper_workbench
    ),
) -> dict[str, Any]:
    try:
        decoded = _decode_cursor(cursor)
        payload = repository.trades(
            book=book,
            sleeve=sleeve,
            symbol=symbol,
            instrument_kind=instrument_kind,
            strategy_revision=strategy_revision,
            lifecycle=lifecycle,
            date_from=date_from,
            date_to=date_to,
            lane=lane,
            structure=structure,
            evidence_class=evidence_class,
            reconciliation_status=reconciliation_status,
            limit=limit,
            cursor=decoded,
        )
        if decoded and decoded[3] is not None and payload.get("snapshot_id") != decoded[3]:
            raise ValueError("Paper trade snapshot is no longer available; reload the scope")
        if decoded and decoded[4] is not None and (payload.get("scope") or {}).get("scope_id") != decoded[4]:
            raise ValueError("Paper trade cursor does not match the requested scope")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = payload.pop("rows")
    has_more = bool(payload.pop("has_more", False))
    next_cursor = (
        _encode_cursor(
            rows[-1]["staged_at"],
            rows[-1]["paper_order_id"],
            snapshot_at=payload.get("as_of"),
            snapshot_id=payload.get("snapshot_id"),
            scope_id=(payload.get("scope") or {}).get("scope_id"),
        )
        if rows and has_more
        else None
    )
    return {**payload, "rows": rows, "next_cursor": next_cursor, "has_more": has_more}


@router.get("/api/paper/trades/export")
def paper_trades_export(
    book: str = Query(default="paper", pattern="^paper$"),
    sleeve: str | None = Query(default=None, min_length=1, max_length=64),
    symbol: str | None = Query(default=None, min_length=1, max_length=16),
    instrument_kind: str | None = Query(default=None, min_length=1, max_length=32),
    strategy_revision: int | None = Query(default=None, ge=1),
    lifecycle: str | None = Query(default=None, pattern="^(staged|open|closed)$"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    lane: str | None = Query(default=None, min_length=1, max_length=32),
    structure: str | None = Query(default=None, min_length=1, max_length=64),
    evidence_class: str | None = Query(default=None, pattern="^(verified|partial|unavailable)$"),
    reconciliation_status: str | None = Query(default=None, pattern="^(verified|partial|unavailable)$"),
    export_format: str = Query(default="csv", alias="format", pattern="^(csv|json)$"),
    repository: dependencies.PaperWorkbenchRepository = Depends(
        dependencies.get_paper_workbench
    ),
) -> Response:
    try:
        payload = repository.export_rows(
            book=book,
            sleeve=sleeve,
            symbol=symbol,
            instrument_kind=instrument_kind,
            strategy_revision=strategy_revision,
            lifecycle=lifecycle,
            date_from=date_from,
            date_to=date_to,
            lane=lane,
            structure=structure,
            evidence_class=evidence_class,
            reconciliation_status=reconciliation_status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if export_format == "json":
        return JSONResponse(content=jsonable_encoder(payload))
    output = io.StringIO()
    fields = (
        "paper_order_id", "book", "sleeve", "symbol", "lane", "structure",
        "lifecycle", "decision_at", "staged_at", "filled_quantity",
        "remaining_quantity", "entry_price", "mark_price", "realized_pnl",
        "unrealized_pnl", "net_pnl", "mark_status", "reconciliation_status",
        "evidence_reasons",
    )
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in payload["rows"]:
        writer.writerow({field: row.get(field) for field in fields})
    headers = {
        "Content-Disposition": "attachment; filename=market-paper-trades.csv",
        "X-Market-Snapshot-Id": str(payload.get("snapshot_id") or ""),
        "X-Market-As-Of": str(payload.get("as_of") or ""),
        "X-Market-Source-Watermark": str(payload.get("source_watermark") or ""),
    }
    if payload["total"] > len(payload["rows"]):
        headers["X-Market-Export-Limit"] = str(len(payload["rows"]))
    return Response(content=output.getvalue(), media_type="text/csv", headers=headers)


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
    research_repository: dependencies.ResearchWorkbenchRepository = Depends(
        dependencies.get_research_workbench
    ),
) -> dict[str, Any]:
    performance = repository.performance()
    performance.pop("trades", None)
    advisor = continuous_owner.overview(config)
    strategy_page = research_repository.strategy_revisions(limit=200)
    strategy_rows = strategy_page["rows"]
    active_strategy = next(
        (
            row
            for row in strategy_rows
            if row.get("authority_group") == "options-radar-core"
            and row.get("status") == "active"
        ),
        None,
    )
    active_authority = active_strategy.get("authority_group") if active_strategy else None
    active_id = active_strategy.get("strategy_revision_id") if active_strategy else None
    challenger_strategy = next(
        (
            row
            for row in strategy_rows
            if row.get("status") in {"candidate", "testing", "approved"}
            and row.get("authority_group") == active_authority
            and (
                active_id is None
                or row.get("supersedes_id") is None
                or row.get("supersedes_id") == active_id
            )
        ),
        None,
    )
    enabled = bool(config.agents.thesis_monitor.continuous_enabled)
    prompt_health = advisor.get("strategy_health") or {}
    challenger = prompt_health.get("challenger") or {}
    return {
        "paper_only": True,
        "as_of": performance["as_of"],
        "source_watermark": performance["source_watermark"],
        "paper": performance,
        "strategy_lane": _strategy_lane(
            active=active_strategy,
            challenger=challenger_strategy,
            performance=performance,
            auto_promotion=bool(config.analysis.options_decision_system.strategy_auto_promotion_enabled),
        ),
        "prediction_lane": {
            "status": "disabled" if not enabled else "advisory_only",
            "deployed_version": prompt_health.get("active_prompt_version"),
            "challenger": challenger.get("version"),
            "permitted_automatic_action": "advisory_only",
            "overview": advisor,
            "blockers": [],
        },
        "events": research_repository.events(limit=25),
        "diagnostics": research_repository.diagnostics(),
        "actions": [
            *research_repository.action_items(limit=10),
            *_research_actions(
                performance=performance,
            ),
        ],
    }


def _strategy_lane(
    *,
    active: dict[str, Any] | None,
    challenger: dict[str, Any] | None,
    performance: dict[str, Any],
    auto_promotion: bool,
) -> dict[str, Any]:
    if challenger:
        status = "awaiting_human_review" if challenger.get("status") == "approved" else "collecting_outcomes"
    elif active and int(active.get("evaluation_count") or 0) > 0:
        status = "monitoring"
    elif performance["counts"]["filled_orders"]:
        status = "collecting_outcomes"
    else:
        status = "no_paper_fills"
    blockers = list(performance.get("missing_evidence_reasons") or [])
    if not active:
        blockers.append("no_deployed_strategy_revision")
    return {
        "status": status,
        "deployed_version": active.get("strategy_key") if active else None,
        "challenger": challenger.get("strategy_key") if challenger else None,
        "permitted_automatic_action": "deterministic_policy_gates_only" if auto_promotion else "human_review_required",
        "evidence_counts": performance["counts"],
        "blockers": sorted(set(blockers)),
        "operational_health": "database_read_available",
        "investment_quality": "not_established_by_run_health",
    }


def _research_actions(
    *,
    performance: dict[str, Any],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if performance.get("counts", {}).get("filled_orders", 0) and performance.get("missing_evidence_reasons"):
        actions.append({
            "kind": "paper_reconciliation",
            "severity": "high",
            "title": "Paper accounting evidence needs attention",
            "explanation": ", ".join(performance["missing_evidence_reasons"]),
            "evidence": performance.get("snapshot_id"),
            "permitted_action": "inspect_paper_book",
            "drill_down": "/portfolio/paper",
            "postcondition": "Review the scoped accounting evidence and resolve or acknowledge each gap.",
        })
    return actions


def _decode_cursor(cursor: str | None) -> tuple[datetime, str, datetime | None, str | None, str | None] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
        at = datetime.fromisoformat(str(payload["staged_at"]))
        trade_id = str(UUID(str(payload["paper_order_id"])))
        snapshot_at = (
            datetime.fromisoformat(str(payload["snapshot_at"]))
            if payload.get("snapshot_at")
            else None
        )
        snapshot_id = str(payload["snapshot_id"]) if payload.get("snapshot_id") else None
        scope_id = str(payload["scope_id"]) if payload.get("scope_id") else None
    except (
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("Invalid paper trade cursor") from exc
    if (
        at.tzinfo is None
        or snapshot_at is not None and snapshot_at.tzinfo is None
        or snapshot_id is not None and not 1 <= len(snapshot_id) <= 128
        or scope_id is not None and not 1 <= len(scope_id) <= 128
    ):
        raise ValueError("Invalid paper trade cursor")
    return at, trade_id, snapshot_at, snapshot_id, scope_id


def _encode_cursor(
    staged_at: datetime,
    paper_order_id: str,
    *,
    snapshot_at: datetime | None = None,
    snapshot_id: str | None = None,
    scope_id: str | None = None,
) -> str:
    raw = json.dumps(
        {
            "staged_at": staged_at.isoformat(),
            "paper_order_id": paper_order_id,
            "snapshot_at": snapshot_at.isoformat() if snapshot_at else None,
            "snapshot_id": snapshot_id,
            "scope_id": scope_id,
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


__all__ = ["router"]
