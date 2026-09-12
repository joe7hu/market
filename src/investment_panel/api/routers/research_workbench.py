"""Read-only strategy, prediction, prompt, and experiment workbench routes."""

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
    ContinuousAdvisorRunDetailResponse,
    ResearchClaimDetail,
    ResearchClaimPage,
    ResearchArtifactDetail,
    ResearchExperimentDetail,
    ResearchExperimentPage,
    ResearchEventPage,
    ResearchPromptDetail,
    ResearchPromptPage,
    ResearchStrategyDetail,
    ResearchStrategyPage,
)
from investment_panel.settings import AppConfig


router = APIRouter()


@router.get("/api/research/strategies", response_model=ResearchStrategyPage, response_model_exclude_unset=True)
def research_strategies(
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1024),
    repository: dependencies.ResearchWorkbenchRepository = Depends(dependencies.get_research_workbench),
) -> dict[str, Any]:
    try:
        decoded = _decode_strategy_cursor(cursor)
        payload = repository.strategy_revisions(limit=limit, cursor=decoded)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = payload["rows"]
    payload["next_cursor"] = _encode_strategy_cursor(rows[-1]) if len(rows) == limit else None
    return payload


@router.get("/api/research/strategies/{revision_id}", response_model=ResearchStrategyDetail, response_model_exclude_unset=True)
def research_strategy(
    revision_id: int,
    repository: dependencies.ResearchWorkbenchRepository = Depends(dependencies.get_research_workbench),
) -> dict[str, Any]:
    detail = repository.strategy_revision(revision_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Strategy revision not found")
    return detail


@router.get("/api/research/predictions", response_model=ResearchClaimPage, response_model_exclude_unset=True)
def research_predictions(
    symbol: str | None = Query(default=None, min_length=1, max_length=16),
    prompt_version: str | None = Query(default=None, min_length=1, max_length=200),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1024),
    repository: dependencies.ResearchWorkbenchRepository = Depends(dependencies.get_research_workbench),
) -> dict[str, Any]:
    try:
        decoded = _decode_claim_cursor(cursor)
        payload = repository.forecast_claims(
            symbol=symbol,
            prompt_version=prompt_version,
            limit=limit,
            cursor=decoded,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = payload["rows"]
    payload["next_cursor"] = _encode_claim_cursor(rows[-1]) if len(rows) == limit else None
    return payload


@router.get("/api/research/predictions/{claim_id}", response_model=ResearchClaimDetail, response_model_exclude_unset=True)
def research_prediction(
    claim_id: str,
    repository: dependencies.ResearchWorkbenchRepository = Depends(dependencies.get_research_workbench),
) -> dict[str, Any]:
    detail = repository.forecast_claim(claim_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Forecast claim not found")
    return detail


@router.get("/api/research/prompts", response_model=ResearchPromptPage, response_model_exclude_unset=True)
def research_prompts(
    config: AppConfig = Depends(dependencies.get_config),
    repository: dependencies.ResearchWorkbenchRepository = Depends(dependencies.get_research_workbench),
) -> dict[str, Any]:
    return repository.prompt_versions(default_version=config.agents.thesis_monitor.prompt_version)


@router.get("/api/research/prompts/{version}", response_model=ResearchPromptDetail, response_model_exclude_unset=True)
def research_prompt(
    version: str,
    config: AppConfig = Depends(dependencies.get_config),
    repository: dependencies.ResearchWorkbenchRepository = Depends(dependencies.get_research_workbench),
) -> dict[str, Any]:
    detail = repository.prompt_version(version, default_version=config.agents.thesis_monitor.prompt_version)
    if detail is None:
        raise HTTPException(status_code=404, detail="Prompt version not found")
    return detail


@router.get("/api/research/experiments", response_model=ResearchExperimentPage, response_model_exclude_unset=True)
def research_experiments(
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1024),
    repository: dependencies.ResearchWorkbenchRepository = Depends(dependencies.get_research_workbench),
) -> dict[str, Any]:
    try:
        decoded = _decode_experiment_cursor(cursor)
        payload = repository.experiments(limit=limit, cursor=decoded)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = payload["rows"]
    payload["next_cursor"] = _encode_experiment_cursor(rows[-1]) if len(rows) == limit else None
    return payload


@router.get("/api/research/experiments/{experiment_id}", response_model=ResearchExperimentDetail, response_model_exclude_unset=True)
def research_experiment(
    experiment_id: str,
    repository: dependencies.ResearchWorkbenchRepository = Depends(dependencies.get_research_workbench),
) -> dict[str, Any]:
    detail = repository.experiment(experiment_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Research experiment not found")
    return detail


@router.get("/api/research/events", response_model=ResearchEventPage, response_model_exclude_unset=True)
def research_events(
    limit: int = Query(default=50, ge=1, le=100),
    repository: dependencies.ResearchWorkbenchRepository = Depends(dependencies.get_research_workbench),
) -> dict[str, Any]:
    rows = repository.events(limit=limit)
    return {"rows": rows, "count": len(rows)}


@router.get("/api/research/artifacts/{artifact_id}", response_model=ResearchArtifactDetail, response_model_exclude_unset=True)
def research_artifact(
    artifact_id: str,
    repository: dependencies.ResearchWorkbenchRepository = Depends(dependencies.get_research_workbench),
) -> dict[str, Any]:
    detail = repository.artifact(artifact_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Research artifact not found")
    return detail


@router.get("/api/research/runs/{run_id}", response_model=ContinuousAdvisorRunDetailResponse, response_model_exclude_unset=True)
def research_run(
    run_id: str,
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    detail = continuous_owner.run_detail(config, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Research run not found")
    return detail


def _decode_claim_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        claim_id = str(UUID(str(payload["claim_id"])))
    except (binascii.Error, KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid research prediction cursor") from exc
    if created_at.tzinfo is None:
        raise ValueError("Invalid research prediction cursor")
    return created_at, claim_id


def _decode_strategy_cursor(cursor: str | None) -> tuple[int, datetime, int] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        active_rank = int(payload["active_rank"])
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        revision_id = int(payload["strategy_revision_id"])
    except (binascii.Error, KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid research strategy cursor") from exc
    if active_rank not in {0, 1} or created_at.tzinfo is None or revision_id < 1:
        raise ValueError("Invalid research strategy cursor")
    return active_rank, created_at, revision_id


def _encode_strategy_cursor(row: dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "active_rank": 1 if row.get("status") == "active" else 0,
            "created_at": row["created_at"],
            "strategy_revision_id": row["strategy_revision_id"],
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _encode_claim_cursor(row: dict[str, Any]) -> str:
    raw = json.dumps(
        {"created_at": row["issued_at"], "claim_id": row["claim_id"]},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_experiment_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        observed_at = datetime.fromisoformat(str(payload["observed_at"]))
        experiment_id = str(payload["experiment_id"])
        if not experiment_id:
            raise ValueError
    except (binascii.Error, KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid research experiment cursor") from exc
    if observed_at.tzinfo is None:
        raise ValueError("Invalid research experiment cursor")
    return observed_at, experiment_id


def _encode_experiment_cursor(row: dict[str, Any]) -> str:
    raw = json.dumps(
        {"observed_at": row["observed_at"], "experiment_id": row["experiment_id"]},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


__all__ = ["router"]
