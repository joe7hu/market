"""Public typed contracts for the historical option-chain API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

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
    quality_status: str | None = None
    evidence_classification: str | None = None
    evidence_blockers: list[str]


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
    readiness: "OptionsDecisionReadiness"
    strongest_candidate: "OptionsDecisionCandidate | None" = None
    paper_only: bool


class OptionBlockerCount(BaseModel):
    blocker: str
    count: int


class OptionsCaptureReadiness(BaseModel):
    capture_state: str | None = None
    completeness: float | None = None
    capture_generation_id: int | None = None
    complete_captures: int


class OptionsUnderlyingReadiness(BaseModel):
    group_count: int
    groups_with_missing_underlying: int
    groups_with_inconsistent_underlying: int


class OptionsAnalysisReadiness(BaseModel):
    eligible_groups: int
    fit_attempts: int
    succeeded_groups: int
    solver_failures: int


class OptionsThesisReadiness(BaseModel):
    eligible: bool
    revision: str | None = None
    invalidation: str | None = None


class OptionsCalibrationReadiness(BaseModel):
    structure: str
    market_regime: str | None = None
    model_revision: str
    mature_outcomes: int
    lower_95_expectancy: float | None = None
    brier_score: float | None = None
    missing_prerequisites: list[str]


class OptionsCanaryReadiness(BaseModel):
    observed_regular_session_dates: int
    qualified_regular_sessions: int
    required_regular_sessions: int
    canary_revision: str
    canary_started_at: datetime | None = None
    disqualification_reasons: list[dict[str, Any]]


class OptionsDecisionReadiness(BaseModel):
    capture: OptionsCaptureReadiness
    underlying: OptionsUnderlyingReadiness
    analysis: OptionsAnalysisReadiness
    thesis: OptionsThesisReadiness
    calibration: list[OptionsCalibrationReadiness]
    canary: OptionsCanaryReadiness
    top_blockers: list[OptionBlockerCount]
    next_required_action: str


class OptionCandidateLeg(BaseModel):
    contract_id: int
    option_type: str
    side: str
    strike: float
    bid: float | None = None
    ask: float | None = None
    observed_at: datetime | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    open_interest: int | None = None
    volume: int | None = None
    provider_iv: float | None = None
    provider_delta: float | None = None


class OptionTradeTicketLeg(BaseModel):
    contract_id: str
    option_type: str
    side: str
    strike: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    quote_time: datetime | None = None
    quote_age_seconds: float | None = None
    open_interest: int | None = None
    volume: int | None = None


class RecoveryOptionTradeTicketLeg(OptionTradeTicketLeg):
    """v4 recovery legs retain the exact OCC identity alongside the catalog ID."""

    occ_symbol: str


class OptionTradeTicket(BaseModel):
    ticket_version: int
    decision_id: str
    symbol: str
    state: str
    structure: str
    expiration: date
    legs: list[OptionTradeTicketLeg]
    entry: dict[str, Any]
    risk: dict[str, Any]
    thesis: dict[str, Any]
    exits: dict[str, Any]
    forecast: dict[str, Any]
    lower_confidence_expectancy_per_max_risk: float | None = None
    blockers: list[str]
    required_next_action: str
    data_model_revisions: dict[str, Any]
    provenance: dict[str, Any]
    paper_only: bool


class RecoveryOptionTradeTicketV4(BaseModel):
    """Forward recovery ticket; intentionally distinct from the v3 radar ticket."""

    ticket_version: Literal[4]
    objective_version: str
    decision_id: str
    event_id: str
    symbol: str
    family: str
    state: str
    structure: str
    expiration: date
    legs: list[RecoveryOptionTradeTicketLeg]
    entry: dict[str, Any]
    risk: dict[str, Any]
    invalidation: str
    exit_ladder: dict[str, Any]
    forecast: dict[str, Any]
    blockers: list[str]
    paper_only: bool
    live_order_submission: bool


class OptionsDecisionCandidate(BaseModel):
    decision_id: str
    relative_value_id: int
    paper_state: str
    discovery_lane: str
    structure: str
    expiration: date
    strike: float
    option_type: str
    legs: list[OptionCandidateLeg]
    conservative_entry: dict[str, Any]
    one_unit_max_loss: float | None = None
    fair_value_interval: dict[str, float | None]
    expected_value_interval: dict[str, float | None]
    uncertainty: dict[str, float | None]
    modeled_net_edge: float | None = None
    quote_quality: dict[str, Any]
    liquidity: dict[str, Any]
    thesis: dict[str, Any]
    state_reasons: list[str]
    blockers: list[str]
    reassessment_date: date | None = None
    comparable_exact_structure_outcomes: dict[str, Any]
    ticket: OptionTradeTicket | None = None
    paper_only: bool


class OptionsCandidatePage(BaseModel):
    items: list[OptionsDecisionCandidate]
    total: int
    next_cursor: str | None = None
    as_of: datetime | None = None
    capture_generation_id: int | None = None
    model_revision: str
    scope: str
    analysis_run_id: str | None = None
    # Compatibility aliases for the existing frontend release.
    rows: list[OptionsDecisionCandidate]
    count: int
    offset: int
    limit: int


class OptionsPaperJournalRow(BaseModel):
    record_kind: str
    paper_order_id: str | None = None
    shadow_id: str | None = None
    decision_id: str
    lifecycle: str
    structure: str | None = None
    entry_at: datetime | None = None
    conservative_entry_price: float | None = None
    conservative_fill_basis: str | None = None
    latest_mark: float | None = None
    missing_mark_gap: bool
    current_return: float | None = None
    outcome_state: str | None = None
    pending_entry_reason: str | None = None
    assignment_warning: str | None = None
    admission: dict[str, Any]
    contract: dict[str, Any]
    thesis: dict[str, Any]
    forecast: dict[str, Any]
    execution: dict[str, Any]
    outcome: dict[str, Any]
    metrics: dict[str, Any]


class OptionsPaperJournalPage(BaseModel):
    rows: list[OptionsPaperJournalRow]
    count: int
    offset: int
    limit: int


class OptionsLearningProgress(BaseModel):
    structure: str
    market_regime: str | None = None
    model_revision: str
    mature_outcomes: int
    required_mature_outcomes: int
    lower_95_expectancy: float | None = None
    brier_score: float | None = None
    missing_prerequisites: list[str]


class OptionsLearningProgressPage(BaseModel):
    rows: list[OptionsLearningProgress]
    count: int


OptionsDecisionBrief.model_rebuild()
