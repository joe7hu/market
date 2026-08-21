import type {
  DashboardPayload,
  PanelData,
  JsonValue,
  RowRecord,
  SettingsPayload,
  TablePayload,
  TickerPayload,
} from "./types";
import { EMPTY_TABLE, emptyPanelData, mergeSnapshot, type PanelSnapshotPayload } from "./apiPanelData";

export { emptyPanelData } from "./apiPanelData";

export type RefreshJob = {
  id?: string;
  job_name?: string;
  status?: "running" | "succeeded" | "failed" | string;
  started_at?: string;
  finished_at?: string | null;
  error?: string | null;
  summary?: JsonValue;
};

export type RefreshJobsPayload = {
  rows?: RefreshJob[];
  count?: number;
  allowlist?: string[];
  latest_status?: {
    ok?: boolean;
    status?: string;
    // Data freshness independent of the housekeeping tail (snapshot/prune): a
    // failed snapshot still leaves the panel's data fully refreshed.
    dataOk?: boolean;
    dataFinishedAt?: string | null;
    startedAt?: string;
    finishedAt?: string;
    failedStep?: string | null;
    job?: string;
    host?: string;
  } | null;
};

export type OptionHistorySnapshot = {
  snapshot_id: number; symbol: string; slot_at: string | null; observed_at: string;
  capture_started_at: string | null; capture_finished_at: string | null;
  expected_contract_count: number | null; received_contract_count: number | null;
  completeness: number | null; capture_state: string; contract_count: number;
};

export type OptionHistoryChainRow = {
  snapshot_id: number; symbol: string; slot_at: string | null; contract_id: number;
  expiration: string; strike: number; option_type: "call" | "put"; dte: number;
  log_moneyness: number | null; underlying_price: number | null; bid: number | null; ask: number | null;
  mid: number | null; last: number | null; previous_close: number | null; bid_size: number | null; ask_size: number | null;
  provider_iv: number | null; provider_delta: number | null; provider_gamma: number | null; provider_theta: number | null;
  provider_vega: number | null; provider_rho: number | null; volume: number | null; open_interest: number | null;
  chance_of_profit_long: number | null; chance_of_profit_short: number | null; market_data_status: string | null;
  quality_status: string | null; evidence_classification: string | null; evidence_blockers: string[];
};

export type OptionHistoryPage<T> = { rows: T[]; count: number; offset: number; limit: number; snapshot_id?: number | null };
export type OptionHistorySurface = { snapshot_id: number | null; symbol: string; expiration: string; option_type: "call" | "put"; observed: Array<Record<string, unknown>>; fitted: Array<Record<string, unknown>>; uncertainty: Array<Record<string, unknown>>; fit_status: string; diagnostics: Record<string, unknown> };
export type OptionHistorySurfaceGrid = {
  snapshot_id: number | null;
  symbol: string;
  x: number[];
  y: number[];
  surfaces: Partial<Record<"call" | "put", Array<Array<number | null>>>>;
  observed: Array<Record<string, unknown>>;
};
export type OptionHistorySurfaceGroup = { expiration: string; option_type: "call" | "put"; dte: number; contract_count: number };
export type OptionHistorySurfaceGroups = { snapshot_id: number | null; rows: OptionHistorySurfaceGroup[] };
export type OptionHistoryCurves = { snapshot_id: number | null; smiles: Array<Record<string, unknown>>; term_structure: Array<Record<string, unknown>>; history: Array<Record<string, unknown>>; history_state: string };
export type OptionHistoryAnomaly = { id: number; snapshot_id: number; contract_id: number | null; expiration: string | null; option_type: string | null; anomaly_type: string; state: string; observed_value: number | null; expected_value: number | null; z_score: number | null; details: Record<string, unknown>; created_at: string; strike: number | null };
export type OptionEventStudy = {
  ticker: string; event_kind: string; as_of: string; evidence_state: string;
  rows: Array<{ id: string; event_starts_at: string; event_session: string; horizon: number; sample_size: number; actual_move_median: number | null; actual_move_p75: number | null; actual_move_p90: number | null; bootstrap_low: number | null; bootstrap_high: number | null; implied_move: number | null; evidence_state: string; details: Record<string, unknown> }>;
};
export type OptionDistributionShift = {
  symbol: string; as_of: string; previous_as_of: string | null; feature_version: string; tenors: number[];
  w1_shift: number | null; tail_mass_change: number | null; skew_shift: number | null; term_shift: number | null;
  evidence_state: string; details: Record<string, unknown>; explanation_only: boolean; strategy_effect: boolean;
};
export type OptionRelativeValue = { id: number; analysis_run_id: string; capture_generation_id: number; classification: string; verification_status: string | null; verified_at: string | null; fair_low: number | null; fair_high: number | null; modeled_net_edge: number | null; edge_side: string | null; confidence: number | null; quality_status: string; blockers: string[]; evidence: Record<string, unknown>; contract_id: number; expiration: string; strike: number; option_type: "call" | "put"; snapshot_id: number };
export type OptionHistoryHealth = {
  snapshots: number; complete_captures: number; post_fix_complete_captures: number;
  observed_regular_session_dates: number; qualified_regular_sessions: number;
  required_regular_sessions: number; canary_revision: string; canary_started_at: string | null;
  disqualification_reasons: Array<{ reason: string; count: number }>;
  latest_complete_slot: string | null; average_completeness: number | null;
  option_quote_bytes: number; surface_summary_bytes: number; storage_bytes: number; retention_days: number;
};
export type OptionHistorySymbolPolicy = {
  instrument_id: number;
  symbol: string;
  requested_state: "on" | "off";
  effective_state: "disabled" | "pending_gate" | "shadow" | "active" | "paused";
  collection_tier: "core" | "standard";
  cadence_minutes: 15 | 60;
  publication_cap: "WATCH" | "PAPER_READY";
  provider: string;
  normalized_retention_days: number;
  derived_retention_days: number;
  provider_payload_retention_days: number;
  policy_revision: string;
  lock_version: number;
  reason: string | null;
  latest_complete_capture: string | null;
  latest_snapshot_id: number | null;
  complete_captures: number;
  readiness: string;
};
export type OptionHistorySymbolsPayload = { rows: OptionHistorySymbolPolicy[]; count: number; policy_revision: string };
export type OptionsDecisionState = "COLLECTING" | "WATCH" | "PAPER_READY" | "REJECT";
export type OptionsDecisionReadiness = {
  capture: { capture_state: string | null; completeness: number | null; capture_generation_id: number | null; complete_captures: number };
  underlying: { group_count: number; groups_with_missing_underlying: number; groups_with_inconsistent_underlying: number };
  analysis: { eligible_groups: number; fit_attempts: number; succeeded_groups: number; solver_failures: number };
  thesis: {
    eligible: boolean;
    present?: boolean;
    revision: string | null;
    direction?: string | null;
    blocker?: string | null;
    invalidation: string | null;
  };
  calibration: Array<{ structure: string; market_regime: string | null; model_revision: string; mature_outcomes: number; lower_95_expectancy: number | null; brier_score: number | null; missing_prerequisites: string[] }>;
  canary: { observed_regular_session_dates: number; qualified_regular_sessions: number; required_regular_sessions: number; canary_revision: string; canary_started_at: string | null; disqualification_reasons: Array<{ reason: string; count: number }> };
  top_blockers: Array<{ blocker: string; count: number }>;
  next_required_action: string;
};
export type OptionsCandidateLeg = { contract_id: number; option_type: "call" | "put"; side: "long" | "short"; strike: number; bid: number | null; ask: number | null; observed_at: string | null; bid_size: number | null; ask_size: number | null; open_interest: number | null; volume: number | null; provider_iv: number | null; provider_delta: number | null };
export type OptionTradeTicketLeg = {
  contract_id: string; option_type: "call" | "put" | string; side: "buy" | "sell";
  strike: number | null; bid: number | null; ask: number | null; bid_size: number | null; ask_size: number | null;
  quote_time: string | null; quote_age_seconds: number | null; open_interest: number | null; volume: number | null;
};
export type OptionTradeTicket = {
  ticket_version: number; decision_id: string; symbol: string; state: "READY" | "RESEARCH" | "AUDIT_ONLY";
  structure: string; expiration: string; legs: OptionTradeTicketLeg[];
  entry: { limit_price: number | null; maximum_chase_price: number | null; minimum_credit: number | null; valid_until: string; validity_seconds: number; expected_slippage: number | null };
  risk: { sleeve_capital: number | null; broker_available_capital: number | null; one_unit_max_loss: number | null; one_unit_collateral: number | null; available_risk_budget: number; recommended_quantity: number; total_risk: number; symbol_exposure_after_entry: number; total_options_exposure_after_entry: number; fully_cash_secured: boolean; blockers: string[] };
  thesis: { summary: string | null; catalyst: string | null; invalidation: string | null };
  exits: { profit_price: number | null; loss_price: number | null; time_exit_dte: number; thesis_invalidation: string | null; liquidity_exit: string };
  forecast: { interval: unknown; expected_value: number | null; lower_confidence_expected_value: number | null; probability_profit: number | null; probability_semantics: string | null; effective_sample_size: number | null; tail_loss: number | null; no_trade_expected_value: 0 };
  lower_confidence_expectancy_per_max_risk: number | null; blockers: string[]; required_next_action: string;
  data_model_revisions: Record<string, unknown>; provenance: Record<string, unknown>; paper_only: true;
};
export type OptionsDecisionCandidate = {
  decision_id: string; relative_value_id: number; paper_state: OptionsDecisionState; discovery_lane: "thesis" | "anomaly"; structure: "long_call" | "long_put" | "call_debit_spread" | "put_debit_spread"; expiration: string; strike: number; option_type: "call" | "put";
  legs: OptionsCandidateLeg[]; conservative_entry: { price: number | null; fill_basis: string }; one_unit_max_loss: number | null;
  fair_value_interval: { low: number | null; high: number | null }; expected_value_interval: { expected: number | null; lower_95: number | null };
  uncertainty: { fair_value_width: number | null; data_confidence: number | null; execution_confidence: number | null; relative_value_confidence: number | null };
  modeled_net_edge: number | null; quote_quality: { max_quote_age_seconds: number | null; interleg_skew_seconds: number | null };
  liquidity: { minimum_open_interest?: number | null; minimum_volume?: number | null; displayed_sizes?: Array<{ contract_id: number; bid_size: number | null; ask_size: number | null }> };
  thesis: { id: number | null; revision: string | null; invalidation: string | null; eligible: boolean };
  state_reasons: string[]; blockers: string[]; reassessment_date: string | null;
  comparable_exact_structure_outcomes: { sample_size?: number; lower_95_expectancy?: number | null; brier_score?: number | null; other_regime_monitoring_count?: number };
  strategy_route: StrategyRoute;
  market_regime: MarketRegime;
  ticket: OptionTradeTicket | null; paper_only: boolean;
};
export type StrategyRoute = { route_version?: string; shadow?: boolean; selected_structure: string; alternative_structures?: string[]; trend_state?: string; trend_confidence?: number | null; volatility_state?: string; event_state?: string; selection_reasons?: string[]; rejected_structures?: Array<{ structure: string; reason: string }>; route_blockers: string[]; as_of?: string | null; paper_quantity_authorized?: boolean; ai_can_override?: boolean };
export type MarketRegime = { state?: string; trend_state: string; trend_confidence?: number; volatility_state?: string; breadth_state?: string; quality_status: string; reason_codes?: string[]; as_of?: string | null };
export type DecisionTruth = { symbol: string; lane: string; as_of: string | null; publication_id: string | null; candidate_state: string; route_verdict: string; readiness_state: string; execution_state: string; primary_blocker: string | null; blockers: string[]; next_action: string | null; route_version: string; evidence_refs: Array<Record<string, unknown>> };
export type OptionsDecisionBrief = { symbol: string; lane: "thesis" | "anomaly"; mode: "disabled" | "shadow" | "paper" | string; analysis_run_id: string | null; as_of: string | null; state: OptionsDecisionState; summary: { message?: string; [key: string]: unknown }; readiness: OptionsDecisionReadiness; strongest_candidate: OptionsDecisionCandidate | null; paper_only: boolean; decision_truth?: DecisionTruth };
export type OptionsWorkspacePayload = {
  symbol: string;
  decision_brief: OptionsDecisionBrief;
  capture_generation_id: number | null;
  evidence_as_of: string | null;
  generated_at: string | null;
  freshness_state: string;
  canary_status: OptionsDecisionReadiness["canary"];
  active_revision: string;
  strategy_route: StrategyRoute;
  market_regime: MarketRegime;
  paper_action_capability: { mode: string; enabled: boolean; reason: string };
  tab_counts: {
    candidates: number;
    rejections: number;
    journal: number;
    shadow_observations: number;
    legacy_shadow_observations: number;
  };
};
export type OptionsPaperJournalRow = {
  record_kind: "paper_trade" | "shadow_observation";
  paper_order_id: string | null;
  shadow_id: string | null;
  decision_id: string;
  lifecycle: "pending" | "entered" | "unfilled" | "observing" | "mature" | "expired" | string;
  structure: string | null;
  entry_at: string | null;
  conservative_entry_price: number | null;
  conservative_fill_basis: string | null;
  latest_mark: number | null;
  missing_mark_gap: boolean;
  current_return: number | null;
  outcome_state: string | null;
  pending_entry_reason: string | null;
  assignment_warning: string | null;
  admission: {
    decision_at: string | null; decision_state: string | null; paper_state: string | null;
    discovery_lane: string | null; reasons: string[]; blockers: string[];
    model_revision: string | null; market_regime: string | null;
  };
  contract: {
    expiration: string | null; strike: number | null; option_type: string | null;
    multiplier: number; legs: Array<Record<string, unknown>>;
  };
  thesis: {
    revision: number | null; direction: string | null; core_thesis: string | null;
    invalidation: string | null; horizon_date: string | null;
  };
  forecast: {
    probability_profit: number | null; expected_value: number | null;
    lower_95_expected_value: number | null; max_loss: number | null;
    risk_adjusted_expectancy: number | null; modeled_net_edge: number | null;
    fair_value_low: number | null; fair_value_high: number | null;
    scenario_count: number; data_confidence: number | null; execution_confidence: number | null;
  };
  execution: {
    staged_at: string | null; signal_quote_at: string | null; entry_cohort_id: string | null;
    entry_at: string | null; entry_price: number | null; fill_basis: string | null;
    latest_mark: number | null; exit_at: string | null; exit_price: number | null;
    holding_period_hours: number | null;
  };
  outcome: {
    state: string | null; observed_through: string | null; current_return: number | null;
    return_1d: number | null; return_5d: number | null; return_20d: number | null;
    return_60d: number | null; peak_return: number | null; max_drawdown: number | null;
    realized_exit_return: number | null; realized_exit_basis: string | null;
    attribution: { underlying: number | null; iv: number | null; theta: number | null; spread: number | null; unexplained: number | null };
  };
  metrics: Record<string, unknown>;
};
export type OptionsLearningProgress = { structure: string; market_regime: string | null; model_revision: string; mature_outcomes: number; required_mature_outcomes: number; lower_95_expectancy: number | null; brier_score: number | null; missing_prerequisites: string[] };

// --- Source catalog (GET /api/source-catalog) ------------------------------
export type SourceCatalogRow = {
  source_id: string;
  source_name: string;
  source_family: string;
  source_kind: string;
  operational_group: string;
  enabled: boolean;
  ingestion_mode: string;
  refresh_job: string;
  refresh_jobs: string[];
  cadence_label: string;
  run_status: string;
  freshness_status: string;
  effective_status: string;
  latest_capability: string;
  capability_health: Array<{ capability: string; status: string; finished_at: string | null; failure_detail: string }>;
  last_attempt_at: string | null;
  status_at: string | null;
  last_success_at: string | null;
  last_data_at: string | null;
  item_count: number;
  ticker_count: number;
  failure_detail: string;
  remediation: string;
  inherited_check: boolean;
  source_url: string;
};

export type SourceCatalogPayload = {
  rows: SourceCatalogRow[];
  groups: Record<string, string[]>;
  summary: {
    total: number;
    enabled: number;
    healthy: number;
    attention: number;
    failed: number;
    disabled: number;
    last_success_at: string | null;
  };
  generated_from?: string;
  status?: { ready?: boolean; source?: string; message?: string };
};

export type StrategyPromotionResult = {
  status?: string;
  proposal_id?: string;
  strategy_version?: string;
  approved_by?: string;
};

export type AgentCommandSettingsInput = {
  enabled?: boolean;
  command?: string;
  timeout_seconds?: number;
  limit?: number;
};

export type OptionAgentSettingsInput = {
  enabled?: boolean;
  timeout_seconds?: number;
  thesis_limit?: number;
  postmortem_limit?: number;
  provider?: string;
  model?: string;
  reasoning_effort?: string;
  auto_run_seconds?: number;
  max_runs_per_day?: number;
  context_sources?: Record<string, boolean>;
};

export type AgentSettingsInput = {
  option_thesis?: AgentCommandSettingsInput;
  option_postmortem?: AgentCommandSettingsInput;
  option_agent?: OptionAgentSettingsInput;
};

export type ResearchSourcesInput = {
  x?: {
    enabled?: boolean;
    list_id?: string;
    priority_handles?: string[];
    limit?: number;
    account_fetch_cap?: number;
  };
  news?: {
    enabled?: boolean;
    providers?: string[];
    limit?: number;
  };
  blogs?: {
    enabled?: boolean;
    substack_urls?: string[];
    rss_urls?: string[];
  };
};

export type PanelScopeOptions = {
  offset?: number;
  limit?: number;
  append?: boolean;
  force?: boolean;
};

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${path}${path.includes("?") ? "&" : "?"}_=${Date.now()}`, {
    cache: "no-store",
    signal,
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    const text = await response.text();
    throw new Error(`Expected JSON from ${path}, got ${contentType || "unknown"}: ${text.slice(0, 40)}`);
  }
  return (await response.json()) as T;
}

async function sendJson<T>(path: string, method: "POST" | "PUT" | "DELETE", body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    cache: "no-store",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const text = await response.text();
    let message = text || `${response.status} ${response.statusText}`;
    try {
      const parsed = JSON.parse(text) as { detail?: unknown };
      if (typeof parsed.detail === "string") {
        message = parsed.detail;
      }
    } catch {
      // Keep the raw response text when the server does not return JSON.
    }
    throw new Error(message);
  }
  return (await response.json()) as T;
}

async function patchJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "PATCH",
    cache: "no-store",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const text = await response.text();
    let message = text || `${response.status} ${response.statusText}`;
    try {
      const parsed = JSON.parse(text) as { detail?: unknown };
      if (typeof parsed.detail === "string") {
        message = parsed.detail;
      }
    } catch {
      // Keep the raw response text when the server does not return JSON.
    }
    throw new Error(message);
  }
  return (await response.json()) as T;
}

export async function loadPanelData(): Promise<PanelData> {
  return loadPanelScope("feed");
}

export async function loadPanelScope(scope: string, existing?: PanelData, options: PanelScopeOptions = {}): Promise<PanelData> {
  const params = new URLSearchParams({ scope });
  if (options.offset !== undefined) params.set("offset", String(options.offset));
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  const snapshot = await getJson<PanelSnapshotPayload>(`/api/panel-snapshot?${params.toString()}`);
  const data = mergeSnapshot(existing ?? emptyPanelData(), snapshot, options);
  if (scope === "settings") {
    data.settings = await getJson<SettingsPayload>("/api/settings");
  }
  return data;
}

export async function loadEventScoutSnapshot(signal?: AbortSignal): Promise<PanelSnapshotPayload> {
  const payload = await getJson<{ status?: DashboardPayload["status"]; tables?: Record<string, TablePayload> }>("/api/event-scout", signal);
  return { scope: "event-scout", status: payload.status, tables: payload.tables };
}

export async function loadSuperinvestorPortfolio(investorKey: string, signal?: AbortSignal): Promise<RowRecord> {
  return getJson<RowRecord>(`/api/superinvestors/${encodeURIComponent(investorKey)}`, signal);
}

export async function saveWatchlistSymbol(symbol: string): Promise<TablePayload> {
  const payload = await sendJson<{ watchlist?: TablePayload }>("/api/watchlist/symbols", "POST", {
    symbol,
    asset_class: watchlistAssetClass(symbol),
  });
  return payload.watchlist ?? EMPTY_TABLE;
}

export async function deleteWatchlistSymbol(symbol: string): Promise<TablePayload> {
  const payload = await sendJson<{ watchlist?: TablePayload }>(`/api/watchlist/symbols/${encodeURIComponent(symbol)}`, "DELETE");
  return payload.watchlist ?? EMPTY_TABLE;
}

export async function loadRefreshJobs(): Promise<RefreshJobsPayload> {
  return getJson<RefreshJobsPayload>("/api/refresh-jobs");
}

export async function loadOptionHistorySnapshots(symbol = "QQQ", signal?: AbortSignal): Promise<OptionHistoryPage<OptionHistorySnapshot>> {
  return getJson(`/api/options/history/snapshots?symbol=${encodeURIComponent(symbol)}&limit=500`, signal);
}

export async function loadOptionHistoryChain(params: Record<string, string | number | undefined>, signal?: AbortSignal): Promise<OptionHistoryPage<OptionHistoryChainRow>> {
  return getJson(`/api/options/history/chain?${optionHistoryParams(params)}`, signal);
}

export async function loadOptionHistorySurface(params: Record<string, string | number | undefined>, signal?: AbortSignal): Promise<OptionHistorySurface> {
  return getJson(`/api/options/history/surface?${optionHistoryParams(params)}`, signal);
}

export async function loadOptionHistorySurfaceGrid(params: Record<string, string | number | undefined>, signal?: AbortSignal): Promise<OptionHistorySurfaceGrid> {
  return getJson(`/api/options/history/surface-grid?${optionHistoryParams(params)}`, signal);
}

export async function loadOptionHistorySurfaceGroups(params: Record<string, string | number | undefined>, signal?: AbortSignal): Promise<OptionHistorySurfaceGroups> {
  return getJson(`/api/options/history/surface-groups?${optionHistoryParams(params)}`, signal);
}

export async function loadOptionHistoryCurves(params: Record<string, string | number | undefined>, signal?: AbortSignal): Promise<OptionHistoryCurves> {
  return getJson(`/api/options/history/curves?${optionHistoryParams(params)}`, signal);
}

export async function loadOptionEventStudy(symbol: string, eventKind: string, asOf: string, signal?: AbortSignal): Promise<OptionEventStudy> {
  return getJson(`/api/options/event-study?ticker=${encodeURIComponent(symbol)}&event_kind=${encodeURIComponent(eventKind)}&as_of=${encodeURIComponent(asOf)}`, signal);
}

export async function loadOptionDistributionShift(symbol: string, asOf: string, signal?: AbortSignal): Promise<OptionDistributionShift> {
  return getJson(`/api/options/history/distribution-shift?symbol=${encodeURIComponent(symbol)}&as_of=${encodeURIComponent(asOf)}`, signal);
}

export async function loadOptionHistoryAnomalies(params: Record<string, string | number | undefined>, signal?: AbortSignal): Promise<OptionHistoryPage<OptionHistoryAnomaly>> {
  return getJson(`/api/options/history/anomalies?${optionHistoryParams(params)}`, signal);
}

export async function loadOptionHistoryHealth(): Promise<OptionHistoryHealth> {
  return getJson("/api/options/history/health");
}

export async function loadOptionHistorySymbols(signal?: AbortSignal): Promise<OptionHistorySymbolsPayload> {
  return getJson("/api/options/history/symbols", signal);
}

export async function setWatchlistOptionsHistory(symbol: string, requested_state: "on" | "off", lock_version: number): Promise<{ options_history_policy: OptionHistorySymbolPolicy }> {
  return patchJson(`/api/watchlist/symbols/${encodeURIComponent(symbol)}/options-history`, { requested_state, lock_version });
}

export async function loadOptionsDecisionBrief(symbol = "QQQ", lane: "thesis" | "anomaly" = "thesis", signal?: AbortSignal): Promise<OptionsDecisionBrief> {
  return getJson(`/api/options/decision-brief?symbol=${encodeURIComponent(symbol)}&lane=${lane}`, signal);
}

export async function loadOptionsWorkspace(symbol = "QQQ", lane: "thesis" | "anomaly" = "thesis", signal?: AbortSignal): Promise<OptionsWorkspacePayload> {
  return getJson(`/api/options/workspace?symbol=${encodeURIComponent(symbol)}&lane=${lane}`, signal);
}

export async function loadOptionsCandidates(params: Record<string, string | number | undefined>, signal?: AbortSignal): Promise<OptionHistoryPage<OptionsDecisionCandidate>> {
  return getJson(`/api/options/candidates?${optionHistoryParams(params)}`, signal);
}

export async function loadOptionsRadarLearning(
  collection: string,
  cursor: string | null = null,
  limit = 25,
  signal?: AbortSignal,
): Promise<{ collection: string; items: RowRecord[]; count: number; next_cursor: string | null }> {
  const cursorQuery = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
  return getJson(`/api/options-radar/learning/${encodeURIComponent(collection)}?limit=${limit}${cursorQuery}`, signal);
}

export async function loadOptionTicketDetail(decisionId: string, signal?: AbortSignal): Promise<RowRecord> {
  return getJson<RowRecord>(`/api/options/tickets/${encodeURIComponent(decisionId)}`, signal);
}

export type DecisionInboxPayload = {
  items: RowRecord[];
  count: number;
  next_cursor: string | null;
};

export async function loadDecisionInbox(cursor: string | null = null, signal?: AbortSignal): Promise<DecisionInboxPayload> {
  const query = cursor ? `?limit=50&cursor=${encodeURIComponent(cursor)}` : "?limit=50";
  return getJson<DecisionInboxPayload>(`/api/decision-inbox${query}`, signal);
}

export async function loadOptionsPaperJournal(symbol = "QQQ", signal?: AbortSignal): Promise<OptionHistoryPage<OptionsPaperJournalRow>> {
  return getJson(`/api/options/paper-journal?symbol=${encodeURIComponent(symbol)}&limit=100`, signal);
}

export async function loadOptionsShadowObservations(symbol = "QQQ", signal?: AbortSignal): Promise<OptionHistoryPage<OptionsPaperJournalRow>> {
  return getJson(`/api/options/shadow-observations?symbol=${encodeURIComponent(symbol)}&limit=100`, signal);
}

export async function loadOptionRelativeValues(params: Record<string, string | number | undefined>, signal?: AbortSignal): Promise<OptionHistoryPage<OptionRelativeValue>> {
  return getJson(`/api/options/history/relative-values?${optionHistoryParams(params)}`, signal);
}

export async function loadOptionsLearningProgress(symbol = "QQQ", signal?: AbortSignal): Promise<{ rows: OptionsLearningProgress[]; count: number }> {
  return getJson(`/api/options/learning-progress?symbol=${encodeURIComponent(symbol)}`, signal);
}

function optionHistoryParams(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== undefined && value !== "") search.set(key, String(value));
  return search.toString();
}

export async function loadSourceCatalog(): Promise<SourceCatalogPayload> {
  return getJson<SourceCatalogPayload>("/api/source-catalog");
}

export async function loadSettings(): Promise<SettingsPayload> {
  return getJson<SettingsPayload>("/api/settings");
}

export async function startRefreshJob(jobName: string): Promise<RefreshJob> {
  return sendJson<RefreshJob>(`/api/refresh-jobs/${encodeURIComponent(jobName)}/background`, "POST");
}

export async function updateAgentSettings(payload: AgentSettingsInput): Promise<SettingsPayload> {
  return patchJson<SettingsPayload>("/api/settings/agents", payload);
}

export async function updateResearchSources(payload: ResearchSourcesInput): Promise<SettingsPayload> {
  return patchJson<SettingsPayload>("/api/settings/research-sources", payload);
}

// --- Agent control plane (GET /api/agent, POST /api/agent/analyze) -----------

export type AgentRun = {
  id?: string;
  started_at?: string;
  finished_at?: string;
  trigger?: string;
  workflow?: string;
  ticker?: string | null;
  provider?: string;
  model?: string;
  input_tokens?: number;
  output_tokens?: number;
  tokens_estimated?: boolean;
  est_cost_usd?: number;
  thesis_attempted?: number;
  thesis_accepted?: number;
  postmortem_attempted?: number;
  postmortem_accepted?: number;
  status?: string;
  custom_prompt?: string | null;
};

export type AgentCostWindow = { runs: number; input_tokens: number; output_tokens: number; est_cost_usd: number };

export type DailyResearchPrompt = {
  ready: boolean;
  message: string;
  generated_at: string;
  prompt: string;
  character_count: number;
  estimated_tokens: number;
  coverage: {
    portfolio_positions: number;
    portfolio_symbols: string[];
    watchlist_symbols: number;
    watchlist: string[];
    option_signals: number;
    macro_indicators: number;
    events: number;
    theses: number;
    market_intelligence_items: number;
    future_dated_rows_excluded: number;
  };
  freshness: Array<{ table: string; rows: number; latest_observed?: string | null; future_dated_rows_excluded?: number }>;
};

export type AgentOverview = {
  config: Record<string, unknown>;
  pricing: Record<string, { input_per_1m?: number; output_per_1m?: number }>;
  queue: { thesis_open: number; postmortem_open: number; total_open: number; oldest_open_at?: string | null };
  runs: AgentRun[];
  workflows: Record<string, { runs: number; succeeded: number; failed: number; running: number }>;
  materialization: {
    completed: number;
    materialized: number;
    blocked_manual_lock: number;
    historical_unmaterialized: number;
    historical_superseded: number;
    authority: "research_only";
    recommendation_owner: "canonical_option_trade_ticket";
  };
  cost: { today: AgentCostWindow; last_7d: AgentCostWindow };
  scheduler: { agent_refresh_seconds: number };
};

export async function loadAgent(): Promise<AgentOverview> {
  return getJson<AgentOverview>("/api/agent");
}

export async function loadAgentResearchPrompt(): Promise<DailyResearchPrompt> {
  return getJson<DailyResearchPrompt>("/api/agent/research-prompt");
}

export async function analyzeTicker(ticker: string, prompt?: string): Promise<{ ticker: string; request_id: string; job: RefreshJob }> {
  return sendJson("/api/agent/analyze", "POST", { ticker, prompt });
}

export async function promoteStrategyMutation(proposalId: string, approvedBy = "joe"): Promise<StrategyPromotionResult> {
  return sendJson<StrategyPromotionResult>(
    `/api/strategy-mutation-proposals/${encodeURIComponent(proposalId)}/promote`,
    "POST",
    { approved_by: approvedBy },
  );
}

export async function acknowledgeRadarAlert(alertId: string): Promise<{ status: string; alert_id: string }> {
  return sendJson<{ status: string; alert_id: string }>(`/api/radar-alerts/${encodeURIComponent(alertId)}/ack`, "POST");
}

function watchlistAssetClass(symbol: string): "crypto" | "equity" {
  const normalized = symbol.trim().toUpperCase();
  return normalized.endsWith("-USD") || ["BTC", "ETH", "SOL"].includes(normalized) ? "crypto" : "equity";
}

export async function loadTicker(symbol: string): Promise<TickerPayload> {
  return getJson<TickerPayload>(`/api/tickers/${encodeURIComponent(symbol)}`);
}

export type PortfolioTransactionInput = {
  symbol: string;
  transaction_type: "buy" | "sell";
  quantity: number;
  price: number;
  fees: number;
  executed_at: string;
  notes?: string;
  idempotency_key: string;
  expected_position_version?: string;
};

export type PortfolioTransactionPreview = {
  symbol?: string;
  transaction_type?: string;
  amount?: number;
  fees?: number;
  realized_pnl?: number;
  old_quantity?: number;
  new_quantity?: number;
  old_average_cost?: number;
  new_average_cost?: number;
  position_version: string;
};

export async function previewPortfolioTransaction(transaction: PortfolioTransactionInput): Promise<PortfolioTransactionPreview> {
  return sendJson<PortfolioTransactionPreview>("/api/portfolio/transactions/preview", "POST", transaction);
}

export async function recordPortfolioTransaction(transaction: PortfolioTransactionInput): Promise<{ transaction: RowRecord; portfolio: TablePayload }> {
  return sendJson<{ transaction: RowRecord; portfolio: TablePayload }>("/api/portfolio/transactions", "POST", transaction);
}

export async function reversePortfolioTransaction(transactionId: string, idempotencyKey: string): Promise<{ transaction: RowRecord; portfolio: TablePayload }> {
  return sendJson<{ transaction: RowRecord; portfolio: TablePayload }>(
    `/api/portfolio/transactions/${encodeURIComponent(transactionId)}/reverse`,
    "POST",
    { idempotency_key: idempotencyKey, notes: "Reversed from portfolio activity" },
  );
}

export type ThesisInput = {
  thesis: string;
  why?: string;
  invalidation?: string;
  invalidation_price?: number | null;
  direction?: string | null;
  timeframe?: string | null;
  horizon_date?: string | null;
  conviction?: string | null;
  confidence?: string | null;
  pillars?: RowRecord[];
  scenarios?: RowRecord;
  catalysts?: RowRecord[];
  invalidation_rules?: RowRecord[];
  review_cadence_days?: number | null;
  next_review_date?: string | null;
  lifecycle_status?: string | null;
  evidence_coverage_status?: string | null;
  automation_policy?: "auto" | "manual_lock" | null;
  change_rationale?: string | null;
  status?: string | null;
  evidence_links?: string[];
};

export async function saveThesis(symbol: string, input: ThesisInput): Promise<void> {
  await sendJson<{ thesis: unknown }>(`/api/theses/${encodeURIComponent(symbol)}`, "PUT", input);
}

export async function markThesisReviewed(symbol: string, outcome = "unchanged", notes = ""): Promise<void> {
  await sendJson<{ review: unknown }>(`/api/theses/${encodeURIComponent(symbol)}/review`, "POST", { outcome, notes });
}

export async function getThesisHistory(symbol: string): Promise<RowRecord> {
  return getJson<RowRecord>(`/api/theses/${encodeURIComponent(symbol)}/history`);
}
