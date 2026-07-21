"""Public typed contracts for the historical option-chain API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel


class OptionSnapshotSummary(BaseModel):
    snapshot_id: int
    symbol: str
    slot_at: datetime | None = None
    observed_at: datetime
    capture_started_at: datetime | None = None
    capture_finished_at: datetime | None = None
    expected_contract_count: int | None = None
    received_contract_count: int | None = None
    completeness: float | None = None
    capture_state: str
    contract_count: int


class OptionChainRow(BaseModel):
    snapshot_id: int
    symbol: str
    slot_at: datetime | None = None
    contract_id: int
    expiration: date
    strike: float
    option_type: str
    dte: int
    log_moneyness: float | None = None
    underlying_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    last: float | None = None
    previous_close: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    last_trade_at: datetime | None = None
    captured_at: datetime | None = None
    provider_updated_at: datetime | None = None
    provider_iv: float | None = None
    provider_delta: float | None = None
    provider_gamma: float | None = None
    provider_theta: float | None = None
    provider_vega: float | None = None
    provider_rho: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    chance_of_profit_long: float | None = None
    chance_of_profit_short: float | None = None
    market_data_status: str | None = None


class IVSurfaceGrid(BaseModel):
    snapshot_id: int | None = None
    symbol: str
    x: list[float]
    y: list[int]
    surfaces: dict[str, list[list[float | None]]]
    observed: list[dict[str, Any]]


class OptionSurfaceEvidence(BaseModel):
    snapshot_id: int | None = None
    symbol: str
    expiration: date
    option_type: str
    observed: list[dict[str, Any]]
    fitted: list[dict[str, Any]]
    uncertainty: list[dict[str, Any]]
    fit_status: str
    diagnostics: dict[str, Any]


class OptionSurfaceGroup(BaseModel):
    expiration: date
    option_type: str
    dte: int
    contract_count: int


class OptionSurfaceGroups(BaseModel):
    snapshot_id: int | None = None
    rows: list[OptionSurfaceGroup]


class IVCurveSet(BaseModel):
    snapshot_id: int | None = None
    smiles: list[dict[str, Any]]
    term_structure: list[dict[str, Any]]
    history: list[dict[str, Any]]
    history_state: str


class OptionAnomaly(BaseModel):
    id: int
    snapshot_id: int
    contract_id: int | None = None
    expiration: date | None = None
    option_type: str | None = None
    anomaly_type: str
    state: str
    observed_value: float | None = None
    expected_value: float | None = None
    z_score: float | None = None
    details: dict[str, Any]
    created_at: datetime
    strike: float | None = None


class OptionSnapshotPage(BaseModel):
    rows: list[OptionSnapshotSummary]
    count: int
    offset: int
    limit: int


class OptionChainPage(BaseModel):
    rows: list[OptionChainRow]
    count: int
    offset: int
    limit: int
    snapshot_id: int | None = None


class OptionAnomalyPage(BaseModel):
    rows: list[OptionAnomaly]
    count: int
    offset: int
    limit: int
    snapshot_id: int | None = None


class RelativeValueRow(BaseModel):
    id: int
    analysis_run_id: str
    capture_generation_id: int
    classification: str
    verification_status: str | None = None
    verified_at: datetime | None = None
    fair_low: float | None = None
    fair_high: float | None = None
    modeled_net_edge: float | None = None
    edge_side: str | None = None
    confidence: float | None = None
    quality_status: str
    blockers: list[str]
    evidence: dict[str, Any]
    contract_id: int
    expiration: date
    strike: float
    option_type: str
    snapshot_id: int


class RelativeValuePage(BaseModel):
    rows: list[RelativeValueRow]
    count: int
    offset: int
    limit: int


class OptionsDecisionBrief(BaseModel):
    symbol: str
    lane: str
    mode: str
    analysis_run_id: str | None = None
    as_of: datetime | None = None
    state: str
    summary: dict[str, Any]
    strongest_candidate: dict[str, Any] | None = None
    paper_only: bool


class OptionsCandidatePage(BaseModel):
    rows: list[dict[str, Any]]
    count: int
    offset: int
    limit: int


class OptionsPaperJournalPage(BaseModel):
    rows: list[dict[str, Any]]
    count: int
    offset: int
    limit: int
