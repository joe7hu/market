"""Named HTTP response contracts owned by the application boundary.

The read-model table rows remain dynamic by design. The envelopes around those
rows, domain details, and mutation results are named so FastAPI and the
generated frontend contract have one owner for each browser-facing shape.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.options_history_contracts import (
    OptionsCandidatePage,
    OptionsDecisionBrief,
    OptionsLearningProgressPage,
    OptionsPaperJournalPage,
    OptionAnomalyPage,
    OptionChainPage,
    OptionSnapshotPage,
    OptionSurfaceEvidence,
    OptionSurfaceGroups,
    MarketRegime,
    StrategyRoute,
    IVCurveSet,
    IVSurfaceGrid,
)
from investment_panel.core.decision import (
    CapitalAction,
    DataRequest,
    DecisionResolutionV2,
    ExpressionDecision,
    ExpressionKind,
    TickerDecision,
)


JsonObject = dict[str, Any]
Row = dict[str, Any]


class FlexibleResponse(BaseModel):
    """A named domain response whose provider fields may evolve additively."""

    model_config = ConfigDict(extra="allow")


class ApiStatusResponse(BaseModel):
    ready: bool
    message: str
    source: str
    metadata: JsonObject = Field(default_factory=dict)


class TablePayloadResponse(BaseModel):
    rows: list[Row] = Field(default_factory=list)
    count: int = 0
    offset: int | None = None
    limit: int | None = None
    status: ApiStatusResponse | None = None


class DashboardResponse(BaseModel):
    status: ApiStatusResponse | None = None
    metrics: dict[str, int] = Field(default_factory=dict)
    priority_candidates: list[Row] = Field(default_factory=list)
    model_config = ConfigDict(extra="allow")


class PanelContractResponse(BaseModel):
    scopes: dict[str, list[str]]
    watchlist_section_tables: list[str]
    watchlist_section_output_tables: list[str]
    ticker_tables: list[str]
    frontend_table_keys: dict[str, str]


class PanelSnapshotResponse(BaseModel):
    scope: str
    status: ApiStatusResponse
    dashboard: DashboardResponse | None = None
    tables: dict[str, TablePayloadResponse] = Field(default_factory=dict)


class TodayCapitalAction(FlexibleResponse):
    ticker: str
    action: str
    owned: bool
    rationale: str
    decision_revision: str
    policy_version: str = "risk-policy.v2:legacy"
    resolution: DecisionResolutionV2 | None = None
    selected_expression: str | None = None
    price_condition: str | None = None
    catalyst: str | None = None
    expires_at: date | None = None


class TodayResponse(BaseModel):
    status: ApiStatusResponse
    as_of: datetime | None = None
    actions: list[TodayCapitalAction] = Field(default_factory=list)
    count: int = 0


class TickerBenchmarkResponse(FlexibleResponse):
    status: ApiStatusResponse
    benchmark_key: str | None = None
    as_of: datetime | None = None
    available_at: datetime | None = None
    membership_hash: str | None = None
    member_count: int = 0
    source_id: str | None = None
    source_version: str | None = None
    exact_membership: list[str] = Field(default_factory=list)
    coverage: JsonObject = Field(default_factory=dict)


class RefreshJobResponse(FlexibleResponse):
    id: str | None = None
    job_name: str | None = None
    status: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    scheduled_due_at: datetime | None = None
    dispatched_at: datetime | None = None
    source_status: str | None = None
    downstream_status: str | None = None
    error: str | None = None
    summary: Any = None


class RefreshLatestStatusResponse(FlexibleResponse):
    ok: bool | None = None
    status: str | None = None
    data_ok: bool | None = None
    data_finished_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failed_step: str | None = None
    job: str | None = None
    host: str | None = None


class RefreshJobsResponse(BaseModel):
    rows: list[RefreshJobResponse] = Field(default_factory=list)
    count: int = 0
    allowlist: list[str] = Field(default_factory=list)
    latest_status: RefreshLatestStatusResponse | None = None


class PortfolioTransactionPreviewResponse(FlexibleResponse):
    symbol: str | None = None
    transaction_type: str | None = None
    amount: float | None = None
    fees: float | None = None
    realized_pnl: float | None = None
    old_quantity: float | None = None
    new_quantity: float | None = None
    old_average_cost: float | None = None
    new_average_cost: float | None = None
    position_version: str


class PortfolioTransactionResultResponse(BaseModel):
    transaction: Row
    portfolio: TablePayloadResponse


class WatchlistMutationResponse(BaseModel):
    watchlist_symbol: Row
    data_refresh: Row | None = None
    watchlist: TablePayloadResponse


class OptionsHistoryPolicyResponse(BaseModel):
    options_history_policy: Row


class DecisionInboxResponse(BaseModel):
    items: list[Row] = Field(default_factory=list)
    count: int = 0
    next_cursor: str | None = None


class OptionHistoryHealthResponse(FlexibleResponse):
    snapshots: int = 0
    complete_captures: int = 0
    post_fix_complete_captures: int = 0
    observed_regular_session_dates: int = 0
    qualified_regular_sessions: int = 0
    required_regular_sessions: int = 0
    canary_revision: str = ""
    canary_started_at: datetime | None = None
    disqualification_reasons: list[Row] = Field(default_factory=list)
    latest_complete_slot: datetime | None = None
    average_completeness: float | None = None
    option_quote_bytes: int = 0
    surface_summary_bytes: int = 0
    storage_bytes: int = 0
    retention_days: int = 0
    mode: str | None = None


class OptionHistorySymbolsResponse(BaseModel):
    rows: list[Row] = Field(default_factory=list)
    count: int = 0
    policy_revision: str = ""


class RecoveryHealthResponse(FlexibleResponse):
    scheduler: JsonObject = Field(default_factory=dict)


class RecoveryEventsResponse(BaseModel):
    events: list[Row] = Field(default_factory=list)
    count: int = 0


class RecoveryEventResponse(FlexibleResponse):
    event_id: str | None = None


class OptionsWorkspaceResponse(FlexibleResponse):
    symbol: str
    decision_brief: OptionsDecisionBrief
    capture_generation_id: int | None = None
    evidence_as_of: datetime | None = None
    generated_at: datetime | None = None
    freshness_state: str = "unknown"
    canary_status: JsonObject = Field(default_factory=dict)
    active_revision: str = ""
    strategy_route: StrategyRoute = Field(default_factory=StrategyRoute)
    market_regime: MarketRegime = Field(default_factory=MarketRegime)
    paper_action_capability: JsonObject = Field(default_factory=dict)
    tab_counts: dict[str, int] = Field(default_factory=dict)


class OptionLearningCollectionResponse(BaseModel):
    collection: str
    items: list[Row] = Field(default_factory=list)
    count: int = 0
    next_cursor: str | None = None


class OptionSignalDetailResponse(FlexibleResponse):
    decision_id: str


class OptionTicketDetailResponse(FlexibleResponse):
    ticket_version: int
    decision_id: str
    symbol: str
    state: str
    structure: str
    expiration: date
    legs: list[Row] = Field(default_factory=list)
    ticket: Row
    signal: Row
    publication: Row
    evidence: list[Row] = Field(default_factory=list)
    outcome: Row = Field(default_factory=dict)
    agent_provenance: Row = Field(default_factory=dict)


class PaperEntryResponse(FlexibleResponse):
    status: str | None = None
    decision_id: str | None = None


class TickerPaperEntryResponse(FlexibleResponse):
    status: str
    paper_order_id: str
    ticker: str
    expression_kind: str
    quantity: int
    planned_loss: float
    decision_revision: str
    policy_version: str | None = None
    paper_only: bool = True
    live_order_submission: bool = False


class StaticArbitrageVerificationResponse(FlexibleResponse):
    status: str | None = None
    candidate_id: int | None = None


class OpportunityScorecardResponse(FlexibleResponse):
    lane: str | None = None
    window_days: int | None = None


class AgentOverviewResponse(FlexibleResponse):
    config: JsonObject = Field(default_factory=dict)
    pricing: JsonObject = Field(default_factory=dict)
    queue: "AgentQueueResponse" = Field(default_factory=lambda: AgentQueueResponse())
    runs: list["AgentRunResponse"] = Field(default_factory=list)
    workflows: dict[str, "AgentWorkflowResponse"] = Field(default_factory=dict)
    materialization: "AgentMaterializationResponse" = Field(default_factory=lambda: AgentMaterializationResponse())
    cost: "AgentCostResponse" = Field(default_factory=lambda: AgentCostResponse())
    scheduler: JsonObject = Field(default_factory=dict)


class AgentQueueResponse(FlexibleResponse):
    thesis_open: int = 0
    postmortem_open: int = 0
    total_open: int = 0
    oldest_open_at: datetime | None = None


class AgentRunResponse(FlexibleResponse):
    id: str | None = None
    workflow: str | None = None
    provider: str | None = None
    model: str | None = None
    trigger: str | None = None
    ticker: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    est_cost_usd: float = 0
    status: str | None = None
    tokens_estimated: bool = False
    thesis_attempted: int = 0
    thesis_accepted: int = 0
    postmortem_attempted: int = 0
    postmortem_accepted: int = 0
    error: str | None = None


class AgentWorkflowResponse(FlexibleResponse):
    runs: int = 0
    succeeded: int = 0
    failed: int = 0
    running: int = 0


class AgentMaterializationResponse(FlexibleResponse):
    materialized: int = 0
    completed: int = 0
    historical_unmaterialized: int = 0


class AgentCostWindowResponse(FlexibleResponse):
    runs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    est_cost_usd: float = 0


class AgentCostResponse(FlexibleResponse):
    today: AgentCostWindowResponse = Field(default_factory=AgentCostWindowResponse)
    last_7d: AgentCostWindowResponse = Field(default_factory=AgentCostWindowResponse)


class AgentExperimentResponse(FlexibleResponse):
    status: str | None = None
    advisory_only: bool | None = None
    routing_changed: bool | None = None
    message: str | None = None


class AgentResearchPromptResponse(FlexibleResponse):
    ready: bool
    message: str
    generated_at: datetime | None = None
    prompt: str
    character_count: int
    estimated_tokens: int
    coverage: "AgentResearchCoverageResponse" = Field(default_factory=lambda: AgentResearchCoverageResponse())
    freshness: list["AgentResearchFreshnessResponse"] = Field(default_factory=list)


class AgentResearchCoverageResponse(FlexibleResponse):
    portfolio_positions: int = 0
    portfolio_symbols: list[str] = Field(default_factory=list)
    watchlist_symbols: int = 0
    watchlist: list[str] = Field(default_factory=list)
    option_signals: int = 0
    macro_indicators: int = 0
    events: int = 0
    theses: int = 0
    market_intelligence_items: int = 0
    future_dated_rows_excluded: int = 0


class AgentResearchFreshnessResponse(FlexibleResponse):
    table: str | None = None
    latest_observed: datetime | None = None
    rows: int = 0
    future_rows_excluded: int = 0


class AgentAnalyzeResponse(BaseModel):
    ticker: str
    request_id: str
    job: RefreshJobResponse


class AgentSubmissionResponse(FlexibleResponse):
    status: str
    strategy_version: str | None = None


class StrategyPromotionResponse(FlexibleResponse):
    status: str
    proposal_id: str
    strategy_version: str
    approved_by: str
    radar_refresh: Row | None = None


class RadarAlertAcknowledgementResponse(BaseModel):
    status: str
    alert_id: str


class SourceCatalogResponse(FlexibleResponse):
    rows: list["SourceCatalogRowResponse"] = Field(default_factory=list)
    groups: dict[str, list[str]] = Field(default_factory=dict)
    summary: "SourceCatalogSummaryResponse" = Field(default_factory=lambda: SourceCatalogSummaryResponse())


class SourceCapabilityResponse(FlexibleResponse):
    capability: str = ""
    status: str = ""
    finished_at: datetime | None = None
    failure_detail: str = ""


class SourceCatalogRowResponse(FlexibleResponse):
    source_id: str = ""
    source_name: str = ""
    source_family: str = ""
    source_kind: str = ""
    operational_group: str = "other"
    operational_state: str = "archived"
    enabled: bool = False
    ingestion_mode: str | None = None
    health_owner: str | None = None
    freshness_seconds: int | None = None
    next_due_at: datetime | None = None
    refresh_job: str | None = None
    refresh_jobs: list[str] = Field(default_factory=list)
    cadence_label: str = "event driven"
    run_status: str | None = None
    freshness_status: str | None = None
    effective_status: str = "missing"
    latest_capability: str | None = None
    capability_health: list[SourceCapabilityResponse] = Field(default_factory=list)
    last_attempt_at: datetime | None = None
    status_at: datetime | None = None
    last_success_at: datetime | None = None
    last_data_at: datetime | None = None
    item_count: int = 0
    ticker_count: int = 0
    failure_detail: str = ""
    remediation: str = ""
    inherited_check: bool = False
    source_url: str | None = None


class SourceCatalogSummaryResponse(FlexibleResponse):
    total: int = 0
    enabled: int = 0
    active: int = 0
    standby: int = 0
    archived: int = 0
    healthy: int = 0
    attention: int = 0
    active_attention: int = 0
    failed: int = 0
    disabled: int = 0
    last_success_at: datetime | None = None


class SourceAuditResponse(FlexibleResponse):
    rows: list[Row] = Field(default_factory=list)
    count: int = 0


class SourceDetailResponse(FlexibleResponse):
    source_id: str | None = None


class SuperinvestorDetailResponse(FlexibleResponse):
    investor_key: str | None = None
    holdings: list[Row] = Field(default_factory=list)


class SettingsResponse(BaseModel):
    status: ApiStatusResponse
    config: JsonObject = Field(default_factory=dict)
    sources: SourceAuditResponse | None = None
    agents: JsonObject = Field(default_factory=dict)
    integration: JsonObject = Field(default_factory=dict)


class EventScoutEventsResponse(BaseModel):
    status: ApiStatusResponse
    tables: dict[str, TablePayloadResponse]


class EventScoutPacketsResponse(BaseModel):
    status: ApiStatusResponse
    rows: list[Row] = Field(default_factory=list)
    count: int = 0


class EventScoutReplayResponse(FlexibleResponse):
    status: str
    packet: Row


class EventScoutSignalResponse(FlexibleResponse):
    status: str | None = None
    signal_id: str | None = None


class StorageHealthResponse(FlexibleResponse):
    """Storage observability payload with an explicit outage-only shape."""

    pass


class TickerDetailResponse(FlexibleResponse):
    symbol: str
    ticker: str
    status: ApiStatusResponse
    as_of: datetime | None = None
    dossier: Row = Field(default_factory=dict)
    ticker_decision: TickerDecision
    capital_action: CapitalAction
    resolution: DecisionResolutionV2 | None = None
    policy_version: str = "risk-policy.v2:legacy"
    expressions: dict[ExpressionKind, ExpressionDecision] = Field(default_factory=dict)
    data_requests: list[DataRequest] = Field(default_factory=list)
    learning: JsonObject = Field(default_factory=dict)
    learning_history: list[Row] = Field(default_factory=list)
    decision_revision: str
    found: bool = False


class TickerDecisionSnapshotResponse(TickerDecision):
    """The typed ticker decision; no flexible legacy snapshot fields remain."""


class ThesisMutationResponse(FlexibleResponse):
    thesis: Row
    thesis_monitor: Row | None = None


class ThesisReviewResponse(FlexibleResponse):
    review: Row
    thesis_monitor: Row | None = None


class ThesisHistoryResponse(FlexibleResponse):
    symbol: str | None = None
    revisions: list[Row] = Field(default_factory=list)
    review_events: list[Row] = Field(default_factory=list)


class ThesisAutomationResponse(FlexibleResponse):
    job: Row
    symbols: list[str] | str
    dry_run: bool
    force: bool


class StatusResponse(ApiStatusResponse):
    options_history: OptionHistoryHealthResponse | None = None


class QuotesResponse(TablePayloadResponse):
    pass


class SourceRunSettingsResponse(SettingsResponse):
    pass


AgentOverviewResponse.model_rebuild()
AgentResearchPromptResponse.model_rebuild()
SourceCatalogResponse.model_rebuild()


__all__ = [
    "AgentAnalyzeResponse",
    "AgentExperimentResponse",
    "AgentOverviewResponse",
    "AgentResearchPromptResponse",
    "AgentSubmissionResponse",
    "ApiStatusResponse",
    "DashboardResponse",
    "DecisionInboxResponse",
    "EventScoutEventsResponse",
    "EventScoutPacketsResponse",
    "EventScoutReplayResponse",
    "EventScoutSignalResponse",
    "IVCurveSet",
    "IVSurfaceGrid",
    "OptionAnomalyPage",
    "OptionChainPage",
    "OptionHistoryHealthResponse",
    "OptionHistorySymbolsResponse",
    "OptionLearningCollectionResponse",
    "OptionSignalDetailResponse",
    "OptionSnapshotPage",
    "OptionSurfaceEvidence",
    "OptionSurfaceGroups",
    "OptionTicketDetailResponse",
    "OptionsCandidatePage",
    "OptionsDecisionBrief",
    "OptionsLearningProgressPage",
    "OptionsPaperJournalPage",
    "OptionsHistoryPolicyResponse",
    "OptionsWorkspaceResponse",
    "OpportunityScorecardResponse",
    "PanelContractResponse",
    "PanelSnapshotResponse",
    "PaperEntryResponse",
    "TickerPaperEntryResponse",
    "PortfolioTransactionPreviewResponse",
    "PortfolioTransactionResultResponse",
    "QuotesResponse",
    "RadarAlertAcknowledgementResponse",
    "RecoveryEventResponse",
    "RecoveryEventsResponse",
    "RecoveryHealthResponse",
    "RefreshJobResponse",
    "RefreshJobsResponse",
    "SettingsResponse",
    "SourceCatalogResponse",
    "SourceDetailResponse",
    "SourceRunSettingsResponse",
    "SourceAuditResponse",
    "StaticArbitrageVerificationResponse",
    "StatusResponse",
    "StrategyPromotionResponse",
    "SuperinvestorDetailResponse",
    "TablePayloadResponse",
    "TodayCapitalAction",
    "TodayResponse",
    "ThesisAutomationResponse",
    "ThesisHistoryResponse",
    "ThesisMutationResponse",
    "ThesisReviewResponse",
    "TickerDecisionSnapshotResponse",
    "TickerBenchmarkResponse",
    "TickerDetailResponse",
    "WatchlistMutationResponse",
]
