"""Public typed contracts for the historical option-chain API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from investment_panel.core.decision import DecisionResolutionV2


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


class EventStudyRow(BaseModel):
    id: str
    ticker: str
    event_kind: str
    as_of: datetime
    event_starts_at: datetime
    event_session: str
    horizon: int
    sample_size: int
    actual_move_median: float | None = None
    actual_move_p75: float | None = None
    actual_move_p90: float | None = None
    bootstrap_low: float | None = None
    bootstrap_high: float | None = None
    win_rate: float | None = None
    implied_move: float | None = None
    evidence_state: str
    feature_version: str
    details: dict[str, Any]


class EventStudyResponse(BaseModel):
    ticker: str
    event_kind: str
    as_of: datetime
    evidence_state: str
    rows: list[EventStudyRow]


class DistributionShiftResponse(BaseModel):
    symbol: str
    as_of: datetime
    previous_as_of: datetime | None = None
    feature_version: str
    tenors: list[int]
    w1_shift: float | None = None
    tail_mass_change: float | None = None
    skew_shift: float | None = None
    term_shift: float | None = None
    evidence_state: str
    details: dict[str, Any]
    explanation_only: bool = True
    strategy_effect: bool = False


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


class OptionsDecisionTruth(BaseModel):
    symbol: str
    lane: str
    as_of: datetime | None = None
    publication_id: str | None = None
    candidate_state: str
    route_verdict: str
    readiness_state: str
    execution_state: str
    primary_blocker: str | None = None
    blockers: list[str] = Field(default_factory=list)
    next_action: str | None = None
    route_version: str
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)


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
    decision_truth: OptionsDecisionTruth | None = None


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
    present: bool | None = None
    revision: str | None = None
    invalidation: str | None = None
    blocker: str | None = None
    direction: str | None = None


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


class FlexibleOptionModel(BaseModel):
    """Allow additive evidence fields while typing the fields used by views."""

    model_config = ConfigDict(extra="allow")


class StrategyRoute(FlexibleOptionModel):
    route_version: str | None = None
    shadow: bool = True
    selected_structure: str = "NO_TRADE"
    alternative_structures: list[str] = Field(default_factory=list)
    trend_state: str | None = None
    trend_confidence: float | None = None
    volatility_state: str | None = None
    event_state: str | None = None
    selection_reasons: list[str] = Field(default_factory=list)
    rejected_structures: list[dict[str, Any]] = Field(default_factory=list)
    route_blockers: list[str] = Field(default_factory=list)
    as_of: datetime | None = None
    paper_quantity_authorized: bool = False
    ai_can_override: bool = False


class MarketRegime(FlexibleOptionModel):
    state: str | None = None
    trend_state: str = "unavailable"
    trend_confidence: float | None = None
    volatility_state: str | None = None
    breadth_state: str | None = None
    quality_status: str = "unavailable"
    reason_codes: list[str] = Field(default_factory=list)
    as_of: datetime | None = None


class OptionsCandidateEntry(FlexibleOptionModel):
    price: float | None = None
    fill_basis: str = "worst_side_quote"


class OptionTradeTicketEntry(FlexibleOptionModel):
    limit_price: float | None = None
    maximum_chase_price: float | None = None
    minimum_credit: float | None = None
    valid_until: datetime | None = None
    validity_seconds: int | None = None
    expected_slippage: float | None = None


class OptionTradeTicketRisk(FlexibleOptionModel):
    sleeve_capital: float | None = None
    broker_available_capital: float | None = None
    one_unit_max_loss: float | None = None
    one_unit_collateral: float | None = None
    available_risk_budget: float = 0
    recommended_quantity: int = 0
    total_risk: float = 0
    symbol_exposure_after_entry: float = 0
    total_options_exposure_after_entry: float = 0
    fully_cash_secured: bool = False
    blockers: list[str] = Field(default_factory=list)


class OptionTradeTicketThesis(FlexibleOptionModel):
    summary: str | None = None
    catalyst: str | None = None
    invalidation: str | None = None


class OptionTradeTicketExits(FlexibleOptionModel):
    profit_price: float | None = None
    loss_price: float | None = None
    time_exit_dte: int | None = None
    thesis_invalidation: str | None = None
    liquidity_exit: str | None = None


class OptionTradeTicketForecast(FlexibleOptionModel):
    interval: Any = None
    expected_value: float | None = None
    lower_confidence_expected_value: float | None = None
    probability_profit: float | None = None
    probability_semantics: str | None = None
    effective_sample_size: float | None = None
    tail_loss: float | None = None
    no_trade_expected_value: float = 0


class OptionTradeTicket(BaseModel):
    ticket_version: int
    decision_id: str
    lane: str | None = None
    episode_key: str | None = None
    execution_ready_at: datetime | None = None
    expires_at: datetime | None = None
    risk_policy_version: str | None = None
    policy_version: str | None = None
    decision_revision: str | None = None
    resolution: DecisionResolutionV2 | None = None
    publication_lineage: dict[str, Any] = Field(default_factory=dict)
    symbol: str
    state: str
    structure: str
    expiration: date
    legs: list[OptionTradeTicketLeg] = Field(default_factory=list)
    entry: OptionTradeTicketEntry = Field(default_factory=OptionTradeTicketEntry)
    risk: OptionTradeTicketRisk = Field(default_factory=OptionTradeTicketRisk)
    thesis: OptionTradeTicketThesis = Field(default_factory=OptionTradeTicketThesis)
    exits: OptionTradeTicketExits = Field(default_factory=OptionTradeTicketExits)
    forecast: OptionTradeTicketForecast = Field(default_factory=OptionTradeTicketForecast)
    lower_confidence_expectancy_per_max_risk: float | None = None
    blockers: list[str] = Field(default_factory=list)
    required_next_action: str = "research_only"
    data_model_revisions: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    paper_only: bool = True


class RecoveryOptionTradeTicketV4(BaseModel):
    """Forward recovery ticket; intentionally distinct from the v3 radar ticket."""

    ticket_version: Literal[4]
    objective_version: str
    decision_id: str
    event_id: str
    lane: str | None = None
    episode_key: str | None = None
    execution_ready_at: datetime | None = None
    expires_at: datetime | None = None
    risk_policy_version: str | None = None
    policy_version: str | None = None
    decision_revision: str | None = None
    resolution: DecisionResolutionV2 | None = None
    publication_lineage: dict[str, Any] = Field(default_factory=dict)
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
    conservative_entry: OptionsCandidateEntry = Field(default_factory=OptionsCandidateEntry)
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
    forecast: dict[str, Any] = Field(default_factory=dict)
    execution_ready: bool = False
    strategy_route: StrategyRoute = Field(default_factory=StrategyRoute)
    market_regime: MarketRegime = Field(default_factory=MarketRegime)
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


class OptionsJournalAdmission(FlexibleOptionModel):
    decision_at: datetime | None = None
    decision_state: str | None = None
    paper_state: str | None = None
    discovery_lane: str | None = None
    reasons: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    model_revision: str | None = None
    market_regime: str | None = None


class OptionsJournalContract(FlexibleOptionModel):
    expiration: date | None = None
    strike: float | None = None
    option_type: str | None = None
    multiplier: int = 100
    legs: list[dict[str, Any]] = Field(default_factory=list)


class OptionsJournalThesis(FlexibleOptionModel):
    revision: int | None = None
    direction: str | None = None
    core_thesis: str | None = None
    invalidation: str | None = None
    horizon_date: date | None = None


class OptionsJournalForecast(FlexibleOptionModel):
    probability_profit: float | None = None
    expected_value: float | None = None
    lower_95_expected_value: float | None = None
    max_loss: float | None = None
    risk_adjusted_expectancy: float | None = None
    modeled_net_edge: float | None = None
    fair_value_low: float | None = None
    fair_value_high: float | None = None
    scenario_count: int = 0
    data_confidence: float | None = None
    execution_confidence: float | None = None


class OptionsJournalExecution(FlexibleOptionModel):
    staged_at: datetime | None = None
    signal_quote_at: datetime | None = None
    entry_cohort_id: str | None = None
    entry_at: datetime | None = None
    entry_price: float | None = None
    fill_basis: str | None = None
    latest_mark: float | None = None
    exit_at: datetime | None = None
    exit_price: float | None = None
    holding_period_hours: float | None = None


class OptionsJournalAttribution(FlexibleOptionModel):
    underlying: float | None = None
    iv: float | None = None
    theta: float | None = None
    spread: float | None = None
    unexplained: float | None = None


class OptionsJournalOutcome(FlexibleOptionModel):
    state: str | None = None
    observed_through: datetime | None = None
    current_return: float | None = None
    return_1d: float | None = None
    return_5d: float | None = None
    return_20d: float | None = None
    return_60d: float | None = None
    peak_return: float | None = None
    max_drawdown: float | None = None
    realized_exit_return: float | None = None
    realized_exit_basis: str | None = None
    attribution: OptionsJournalAttribution = Field(default_factory=OptionsJournalAttribution)


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
    admission: OptionsJournalAdmission = Field(default_factory=OptionsJournalAdmission)
    contract: OptionsJournalContract = Field(default_factory=OptionsJournalContract)
    thesis: OptionsJournalThesis = Field(default_factory=OptionsJournalThesis)
    forecast: OptionsJournalForecast = Field(default_factory=OptionsJournalForecast)
    execution: OptionsJournalExecution = Field(default_factory=OptionsJournalExecution)
    outcome: OptionsJournalOutcome = Field(default_factory=OptionsJournalOutcome)
    metrics: dict[str, Any] = Field(default_factory=dict)


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
