"""HTTP request contracts grouped by domain.

Routers use these Pydantic models as transport contracts. Database and action
owners do not import this module.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from investment_panel.database.options_constants import DEFAULT_STRATEGY_VERSION


class PortfolioPositionInput(BaseModel):
    symbol: str
    quantity: float
    avg_cost: float
    purchase_date: str | None = None
    notes: str = ""


class PortfolioTransactionInput(BaseModel):
    symbol: str | None = None
    transaction_type: Literal[
        "opening_balance",
        "buy",
        "sell",
        "dividend",
        "fee",
        "split",
        "transfer_in",
        "transfer_out",
        "cash_deposit",
        "cash_withdrawal",
    ]
    quantity: float | None = None
    price: float | None = None
    amount: float | None = None
    fees: float = 0
    currency: str = "USD"
    account: str = "manual"
    executed_at: str
    notes: str = ""
    idempotency_key: str
    expected_position_version: str | None = None


class PortfolioTransactionReversalInput(BaseModel):
    idempotency_key: str
    notes: str = ""


class ManualAccountReconciliationInput(BaseModel):
    effective_at: str
    cash_balance: float = Field(ge=0, allow_inf_nan=False)
    net_liquidation: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    account: Literal["manual"] = "manual"
    currency: Literal["USD"] = "USD"
    notes: str = ""
    idempotency_key: str = Field(min_length=1, max_length=160)
    expected_reconciliation_version: int | None = Field(default=None, ge=0)


class DecisionInboxStateInput(BaseModel):
    state: Literal["open", "acknowledged", "snoozed", "dismissed", "review_complete"]
    snoozed_until: str | None = None
    dismiss_reason: str | None = Field(default=None, max_length=500)


class WatchlistSymbolInput(BaseModel):
    symbol: str
    name: str | None = None
    asset_class: str = "equity"
    notes: str = ""


class OptionsHistoryToggleInput(BaseModel):
    requested_state: Literal["on", "off"]
    lock_version: int


class ThesisInput(BaseModel):
    thesis: str
    why: str = ""
    invalidation: str = ""
    invalidation_price: float | None = None
    schema_version: Literal[1, 2, 3] = 3
    direction: Literal["bullish", "bearish"] | None = None
    horizon_date: str | None = None
    max_loss: float | None = None
    catalyst: str | None = None
    status: str | None = None
    evidence_links: list[str] | None = None
    timeframe: str | None = None
    conviction: str | None = None
    confidence: str | None = None
    pillars: list[dict[str, Any]] | None = None
    scenarios: dict[str, Any] | None = None
    catalysts: list[dict[str, Any]] | None = None
    invalidation_rules: list[dict[str, Any]] | None = None
    review_cadence_days: int | None = None
    next_review_date: str | None = None
    lifecycle_status: str | None = None
    evidence_coverage_status: str | None = None
    automation_policy: Literal["auto", "manual_lock"] | None = None
    change_rationale: str | None = None

    @model_validator(mode="after")
    def validate_options_v2(self) -> "ThesisInput":
        if self.schema_version == 2:
            if not self.direction or not self.horizon_date or not self.invalidation.strip():
                raise ValueError("schema_version 2 requires direction, horizon_date, and invalidation")
            if self.max_loss is None or self.max_loss <= 0:
                raise ValueError("schema_version 2 requires positive max_loss")
        return self


class ThesisReviewInput(BaseModel):
    outcome: Literal["unchanged", "updated", "invalidated", "closed"] = "unchanged"
    notes: str = ""
    reviewed_evidence_cutoff: str | None = None


class ThesisAutomationInput(BaseModel):
    symbols: list[str] | None = None
    dry_run: bool = False
    force: bool = False


class OptionPaperEntryInput(BaseModel):
    idempotency_key: str
    ticket_version: int = 1
    policy_version: str | None = None
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0, allow_inf_nan=False)


class TickerPaperEntryInput(BaseModel):
    """Unified stock/option paper-entry request for one ticker thesis."""

    idempotency_key: str = Field(min_length=1, max_length=160)
    trade_plan_id: str = Field(min_length=1, max_length=160)
    decision_revision: str = Field(min_length=1, max_length=160)
    policy_version: str | None = Field(default=None, min_length=1, max_length=160)
    expression_kind: Literal[
        "STOCK", "CALL", "PUT", "DEBIT_SPREAD", "CASH_SECURED_PUT"
    ]
    quantity: int | None = Field(default=None, gt=0)
    limit_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class StrategyPromotionInput(BaseModel):
    approved_by: str = "joe"


class OptionAgentSettingsInput(BaseModel):
    enabled: bool | None = None
    command: str | None = None
    timeout_seconds: int | None = None
    thesis_limit: int | None = None
    postmortem_limit: int | None = None
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    auto_run_seconds: int | None = None
    max_runs_per_day: int | None = None
    context_sources: dict[str, bool] | None = None


class ThesisMonitorSettingsInput(BaseModel):
    enabled: bool | None = None
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    prompt_version: str | None = None
    concurrency: int | None = None
    evidence_items_per_symbol: int | None = None
    preopen_enabled: bool | None = None
    material_event_enabled: bool | None = None
    debounce_minutes: int | None = None
    max_material_runs_per_symbol_per_day: int | None = None


class AgentSettingsInput(BaseModel):
    option_agent: OptionAgentSettingsInput | None = None
    thesis_monitor: ThesisMonitorSettingsInput | None = None


class ResearchXSettingsInput(BaseModel):
    enabled: bool | None = None
    list_id: str | None = None
    priority_handles: list[str] | str | None = None
    limit: int | None = None
    account_fetch_cap: int | None = None


class ResearchNewsSettingsInput(BaseModel):
    enabled: bool | None = None
    providers: list[str] | str | None = None
    limit: int | None = None


class ResearchBlogsSettingsInput(BaseModel):
    enabled: bool | None = None
    substack_urls: list[str] | str | None = None
    rss_urls: list[str] | str | None = None


class ResearchSourcesInput(BaseModel):
    x: ResearchXSettingsInput | None = None
    news: ResearchNewsSettingsInput | None = None
    blogs: ResearchBlogsSettingsInput | None = None


class AgentAnalyzeInput(BaseModel):
    ticker: str
    prompt: str | None = None


class TradeJournalInput(BaseModel):
    ticker: str
    contract_id: str
    event_id: str | None = None
    strategy_version: str = DEFAULT_STRATEGY_VERSION
    opportunity: dict[str, Any] = {}
    notes: str = ""
    action: Literal["accepted", "skipped", "entered", "resized", "exited", "invalidated"] = "accepted"
    idempotency_key: str | None = None
    publication_id: str | None = None
    expected_contract_version: int | None = None


__all__ = [
    "AgentAnalyzeInput",
    "AgentSettingsInput",
    "OptionAgentSettingsInput",
    "OptionPaperEntryInput",
    "TickerPaperEntryInput",
    "OptionsHistoryToggleInput",
    "PortfolioPositionInput",
    "PortfolioTransactionInput",
    "PortfolioTransactionReversalInput",
    "ResearchBlogsSettingsInput",
    "ResearchNewsSettingsInput",
    "ResearchSourcesInput",
    "ResearchXSettingsInput",
    "StrategyPromotionInput",
    "ThesisAutomationInput",
    "ThesisInput",
    "ThesisMonitorSettingsInput",
    "ThesisReviewInput",
    "TradeJournalInput",
    "WatchlistSymbolInput",
]
