/** Options history, decision truth, recovery, and paper-only requests. */

import type { components } from "../generated/apiSchema";
import { getJson, patchJson, sendJson } from "../apiTransport";
import type { RowRecord } from "../types";

type ApiSchema = components["schemas"];

export type OptionHistorySnapshot = ApiSchema["OptionSnapshotSummary"];
export type OptionHistoryChainRow = Omit<ApiSchema["OptionChainRow"], "bid" | "ask" | "option_type"> & {
  bid: number | null;
  ask: number | null;
  option_type: "call" | "put";
};
export type OptionHistorySurface = ApiSchema["OptionSurfaceEvidence"];
export type OptionHistorySurfaceGrid = ApiSchema["IVSurfaceGrid"];
export type OptionHistorySurfaceGroup = ApiSchema["OptionSurfaceGroup"];
export type OptionHistorySurfaceGroups = ApiSchema["OptionSurfaceGroups"];
export type OptionHistoryCurves = ApiSchema["IVCurveSet"];
export type OptionHistoryAnomaly = ApiSchema["OptionAnomaly"];
export type OptionEventStudy = ApiSchema["EventStudyResponse"];
export type OptionDistributionShift = ApiSchema["DistributionShiftResponse"];
export type OptionRelativeValue = ApiSchema["RelativeValueRow"];
export type OptionHistoryHealth = ApiSchema["OptionHistoryHealthResponse"];
export type OptionHistorySymbolPolicy = NonNullable<ApiSchema["OptionHistorySymbolsResponse"]["rows"]>[number];
export type OptionHistorySymbolsPayload = ApiSchema["OptionHistorySymbolsResponse"];
export type OptionsDecisionState = "COLLECTING" | "WATCH" | "PAPER_READY" | "REJECT";
export type StrategyRoute = ApiSchema["StrategyRoute"];
export type MarketRegime = ApiSchema["MarketRegime"];
export type OptionTradeTicket = Omit<ApiSchema["OptionTradeTicket"], "legs" | "entry" | "risk" | "thesis" | "exits" | "forecast" | "blockers"> & {
  legs: NonNullable<ApiSchema["OptionTradeTicket"]["legs"]>;
  entry: NonNullable<ApiSchema["OptionTradeTicket"]["entry"]>;
  risk: NonNullable<ApiSchema["OptionTradeTicket"]["risk"]>;
  thesis: NonNullable<ApiSchema["OptionTradeTicket"]["thesis"]>;
  exits: NonNullable<ApiSchema["OptionTradeTicket"]["exits"]>;
  forecast: NonNullable<ApiSchema["OptionTradeTicket"]["forecast"]>;
  blockers: string[];
};
export type OptionsDecisionCandidate = Omit<ApiSchema["OptionsDecisionCandidate"], "strategy_route" | "market_regime" | "forecast" | "execution_ready" | "ticket" | "conservative_entry"> & {
  conservative_entry: NonNullable<ApiSchema["OptionsDecisionCandidate"]["conservative_entry"]>;
  strategy_route: NonNullable<ApiSchema["OptionsDecisionCandidate"]["strategy_route"]>;
  market_regime: NonNullable<ApiSchema["OptionsDecisionCandidate"]["market_regime"]>;
  forecast: NonNullable<ApiSchema["OptionsDecisionCandidate"]["forecast"]>;
  execution_ready: boolean;
  ticket?: OptionTradeTicket | null;
};
export type OptionsDecisionBrief = Omit<ApiSchema["OptionsDecisionBrief"], "strongest_candidate"> & {
  strongest_candidate: OptionsDecisionCandidate | null;
};
export type OptionsWorkspacePayload = Pick<ApiSchema["OptionsWorkspaceResponse"], "symbol" | "active_revision" | "freshness_state"> & {
  decision_brief: OptionsDecisionBrief;
  tab_counts: NonNullable<ApiSchema["OptionsWorkspaceResponse"]["tab_counts"]>;
  capture_generation_id?: ApiSchema["OptionsWorkspaceResponse"]["capture_generation_id"];
  evidence_as_of?: ApiSchema["OptionsWorkspaceResponse"]["evidence_as_of"];
  generated_at?: ApiSchema["OptionsWorkspaceResponse"]["generated_at"];
  strategy_route?: ApiSchema["OptionsWorkspaceResponse"]["strategy_route"];
  market_regime?: MarketRegime;
};
type JournalAdmission = {
  discovery_lane?: string | null;
  paper_state?: string | null;
  model_revision?: string | null;
  market_regime?: string | null;
  decision_at?: string | null;
  blockers: string[];
};
type JournalContract = { expiration?: string | null; strike?: number | null; option_type?: string | null; legs?: Record<string, unknown>[]; multiplier: number };
type JournalThesis = { core_thesis?: string | null; invalidation?: string | null; direction?: string | null; revision?: number | null; horizon_date?: string | null };
type JournalForecast = { probability_profit?: number | null; expected_value?: number | null; lower_95_expected_value?: number | null; max_loss?: number | null; scenario_count: number; execution_confidence?: number | null };
type JournalExecution = { staged_at?: string | null; entry_price?: number | null; latest_mark?: number | null; fill_basis?: string | null; holding_period_hours?: number | null };
type JournalAttribution = { underlying?: number | null; iv?: number | null; theta?: number | null; spread?: number | null };
type JournalOutcome = { current_return?: number | null; return_1d?: number | null; return_5d?: number | null; return_20d?: number | null; return_60d?: number | null; peak_return?: number | null; max_drawdown?: number | null; attribution: JournalAttribution };
export type OptionsPaperJournalRow = ApiSchema["OptionsPaperJournalRow"] & {
  admission: JournalAdmission;
  contract: JournalContract;
  thesis: JournalThesis;
  forecast: JournalForecast;
  execution: JournalExecution;
  outcome: JournalOutcome;
};
export type OptionsLearningProgress = ApiSchema["OptionsLearningProgress"];
export type DecisionInboxPayload = Omit<ApiSchema["DecisionInboxResponse"], "items" | "next_cursor"> & {
  items: RowRecord[];
  next_cursor: string | null;
};
export type StrategyPromotionResult = ApiSchema["StrategyPromotionResponse"];
export type OptionSnapshotPage = ApiSchema["OptionSnapshotPage"];
export type OptionChainPage = Omit<ApiSchema["OptionChainPage"], "rows"> & { rows: OptionHistoryChainRow[] };
export type OptionAnomalyPage = ApiSchema["OptionAnomalyPage"];
export type OptionsJournalPage = Omit<ApiSchema["OptionsPaperJournalPage"], "rows"> & { rows: OptionsPaperJournalRow[] };
export type OptionsCandidatePage = Omit<ApiSchema["OptionsCandidatePage"], "rows" | "items"> & {
  rows: OptionsDecisionCandidate[];
  items: OptionsDecisionCandidate[];
};
export type OptionAgentSettingsInput = ApiSchema["OptionAgentSettingsInput"];

type OptionParams = Record<string, string | number | undefined>;

export async function loadOptionHistorySnapshots(symbol = "QQQ", signal?: AbortSignal): Promise<ApiSchema["OptionSnapshotPage"]> {
  return getJson(`/api/options/history/snapshots?symbol=${encodeURIComponent(symbol)}&limit=500`, signal);
}

export async function loadOptionHistoryChain(params: OptionParams, signal?: AbortSignal): Promise<OptionChainPage> {
  const payload = await getJson<ApiSchema["OptionChainPage"]>(`/api/options/history/chain?${optionParams(params)}`, signal);
  return {
    ...payload,
    rows: payload.rows.map((row) => ({
      ...row,
      bid: row.bid ?? null,
      ask: row.ask ?? null,
      option_type: row.option_type === "put" ? "put" : "call",
    })),
  };
}

export async function loadOptionHistorySurface(params: OptionParams, signal?: AbortSignal): Promise<OptionHistorySurface> {
  return getJson<OptionHistorySurface>(`/api/options/history/surface?${optionParams(params)}`, signal);
}

export async function loadOptionHistorySurfaceGrid(params: OptionParams, signal?: AbortSignal): Promise<OptionHistorySurfaceGrid> {
  return getJson<OptionHistorySurfaceGrid>(`/api/options/history/surface-grid?${optionParams(params)}`, signal);
}

export async function loadOptionHistorySurfaceGroups(params: OptionParams, signal?: AbortSignal): Promise<OptionHistorySurfaceGroups> {
  return getJson<OptionHistorySurfaceGroups>(`/api/options/history/surface-groups?${optionParams(params)}`, signal);
}

export async function loadOptionHistoryCurves(params: OptionParams, signal?: AbortSignal): Promise<OptionHistoryCurves> {
  return getJson<OptionHistoryCurves>(`/api/options/history/curves?${optionParams(params)}`, signal);
}

export async function loadOptionEventStudy(symbol: string, eventKind: string, asOf: string, signal?: AbortSignal): Promise<OptionEventStudy> {
  return getJson<OptionEventStudy>(
    `/api/options/event-study?ticker=${encodeURIComponent(symbol)}&event_kind=${encodeURIComponent(eventKind)}&as_of=${encodeURIComponent(asOf)}`,
    signal,
  );
}

export async function loadOptionDistributionShift(symbol: string, asOf: string, signal?: AbortSignal): Promise<OptionDistributionShift> {
  return getJson<OptionDistributionShift>(
    `/api/options/history/distribution-shift?symbol=${encodeURIComponent(symbol)}&as_of=${encodeURIComponent(asOf)}`,
    signal,
  );
}

export async function loadOptionHistoryAnomalies(params: OptionParams, signal?: AbortSignal): Promise<ApiSchema["OptionAnomalyPage"]> {
  return getJson(`/api/options/history/anomalies?${optionParams(params)}`, signal);
}

export async function loadOptionHistoryHealth(): Promise<OptionHistoryHealth> {
  return getJson<OptionHistoryHealth>("/api/options/history/health");
}

export async function loadOptionHistorySymbols(signal?: AbortSignal): Promise<OptionHistorySymbolsPayload> {
  return getJson<OptionHistorySymbolsPayload>("/api/options/history/symbols", signal);
}

export async function setWatchlistOptionsHistory(
  symbol: string,
  requestedState: "on" | "off",
  lockVersion: number,
): Promise<ApiSchema["OptionsHistoryPolicyResponse"]> {
  return patchJson<ApiSchema["OptionsHistoryPolicyResponse"]>(
    `/api/watchlist/symbols/${encodeURIComponent(symbol)}/options-history`,
    { requested_state: requestedState, lock_version: lockVersion },
  );
}

export async function loadOptionsDecisionBrief(
  symbol = "QQQ",
  lane: "thesis" | "anomaly" = "thesis",
  signal?: AbortSignal,
): Promise<OptionsDecisionBrief> {
  const payload = await getJson<ApiSchema["OptionsDecisionBrief"]>(`/api/options/decision-brief?symbol=${encodeURIComponent(symbol)}&lane=${lane}`, signal);
  return normalizeBrief(payload);
}

export async function loadOptionsWorkspace(
  symbol = "QQQ",
  lane: "thesis" | "anomaly" = "thesis",
  signal?: AbortSignal,
): Promise<OptionsWorkspacePayload> {
  const payload = await getJson<ApiSchema["OptionsWorkspaceResponse"]>(`/api/options/workspace?symbol=${encodeURIComponent(symbol)}&lane=${lane}`, signal);
  return { ...payload, decision_brief: normalizeBrief(payload.decision_brief), tab_counts: payload.tab_counts ?? {} };
}

export async function loadOptionsCandidates(params: OptionParams, signal?: AbortSignal): Promise<OptionsCandidatePage> {
  const payload = await getJson<ApiSchema["OptionsCandidatePage"]>(`/api/options/candidates?${optionParams(params)}`, signal);
  const rows = payload.rows.map(normalizeCandidate);
  const items = payload.items.map(normalizeCandidate);
  return { ...payload, rows, items };
}

export type OptionLearningCollectionPayload = Omit<ApiSchema["OptionLearningCollectionResponse"], "items" | "next_cursor"> & {
  items: RowRecord[];
  next_cursor: string | null;
};

export async function loadOptionsRadarLearning(
  collection: string,
  cursor: string | null = null,
  limit = 25,
  signal?: AbortSignal,
): Promise<OptionLearningCollectionPayload> {
  const cursorQuery = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
  const payload = await getJson<ApiSchema["OptionLearningCollectionResponse"]>(
    `/api/options-radar/learning/${encodeURIComponent(collection)}?limit=${limit}${cursorQuery}`,
    signal,
  );
  return { ...payload, items: (payload.items ?? []) as RowRecord[], next_cursor: payload.next_cursor ?? null };
}

export async function loadOptionTicketDetail(decisionId: string, signal?: AbortSignal): Promise<ApiSchema["OptionTicketDetailResponse"]> {
  return getJson<ApiSchema["OptionTicketDetailResponse"]>(`/api/options/tickets/${encodeURIComponent(decisionId)}`, signal);
}

export async function loadDecisionInbox(cursor: string | null = null, signal?: AbortSignal): Promise<DecisionInboxPayload> {
  const query = cursor ? `?limit=50&cursor=${encodeURIComponent(cursor)}` : "?limit=50";
  const payload = await getJson<ApiSchema["DecisionInboxResponse"]>(`/api/decision-inbox${query}`, signal);
  return {
    ...payload,
    items: (payload.items ?? []) as RowRecord[],
    next_cursor: payload.next_cursor ?? null,
  };
}

export async function loadOptionsPaperJournal(symbol = "QQQ", signal?: AbortSignal): Promise<OptionsJournalPage> {
  const payload = await getJson<ApiSchema["OptionsPaperJournalPage"]>(`/api/options/paper-journal?symbol=${encodeURIComponent(symbol)}&limit=100`, signal);
  return { ...payload, rows: (payload.rows ?? []) as OptionsPaperJournalRow[] };
}

export async function loadOptionsShadowObservations(symbol = "QQQ", signal?: AbortSignal): Promise<OptionsJournalPage> {
  const payload = await getJson<ApiSchema["OptionsPaperJournalPage"]>(`/api/options/shadow-observations?symbol=${encodeURIComponent(symbol)}&limit=100`, signal);
  return { ...payload, rows: (payload.rows ?? []) as OptionsPaperJournalRow[] };
}

export async function loadOptionRelativeValues(params: OptionParams, signal?: AbortSignal): Promise<ApiSchema["RelativeValuePage"]> {
  return getJson(`/api/options/history/relative-values?${optionParams(params)}`, signal);
}

export async function loadOptionsLearningProgress(symbol = "QQQ", signal?: AbortSignal): Promise<ApiSchema["OptionsLearningProgressPage"]> {
  return getJson(`/api/options/learning-progress?symbol=${encodeURIComponent(symbol)}`, signal);
}

export async function loadOptionSignalDetail(decisionId: string, signal?: AbortSignal): Promise<ApiSchema["OptionSignalDetailResponse"]> {
  return getJson<ApiSchema["OptionSignalDetailResponse"]>(`/api/options-radar/signals/${encodeURIComponent(decisionId)}`, signal);
}

export async function stageOptionPaperEntry(
  decisionId: string,
  payload: ApiSchema["OptionPaperEntryInput"],
): Promise<ApiSchema["PaperEntryResponse"]> {
  return sendJson<ApiSchema["PaperEntryResponse"]>(
    `/api/options-radar/signals/${encodeURIComponent(decisionId)}/paper-entry`,
    "POST",
    payload,
  );
}

export async function promoteStrategyMutation(proposalId: string, approvedBy = "joe"): Promise<StrategyPromotionResult> {
  return sendJson<StrategyPromotionResult>(
    `/api/strategy-mutation-proposals/${encodeURIComponent(proposalId)}/promote`,
    "POST",
    { approved_by: approvedBy },
  );
}

export async function submitAgentThesis(payload: Record<string, unknown>): Promise<ApiSchema["AgentSubmissionResponse"]> {
  return sendJson<ApiSchema["AgentSubmissionResponse"]>("/api/agent-thesis", "POST", payload);
}

export async function submitAgentPostmortem(payload: Record<string, unknown>): Promise<ApiSchema["AgentSubmissionResponse"]> {
  return sendJson<ApiSchema["AgentSubmissionResponse"]>("/api/agent-postmortems", "POST", payload);
}

function optionParams(params: OptionParams): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  return search.toString();
}

function normalizeCandidate(candidate: ApiSchema["OptionsDecisionCandidate"]): OptionsDecisionCandidate {
  return {
    ...candidate,
    strategy_route: candidate.strategy_route ?? {
      selected_structure: "NO_TRADE",
      shadow: true,
      paper_quantity_authorized: false,
      ai_can_override: false,
    },
    market_regime: candidate.market_regime ?? {
      trend_state: "unavailable",
      quality_status: "unavailable",
    },
    forecast: candidate.forecast ?? {},
    execution_ready: candidate.execution_ready ?? false,
  } as OptionsDecisionCandidate;
}

function normalizeBrief(brief: ApiSchema["OptionsDecisionBrief"]): OptionsDecisionBrief {
  return {
    ...brief,
    strongest_candidate: brief.strongest_candidate ? normalizeCandidate(brief.strongest_candidate) : null,
  };
}
