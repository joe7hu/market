import type { components } from "../generated/apiSchema";
import { getJson } from "../apiTransport";

type ApiSchema = components["schemas"];

export type PaperTrade = ApiSchema["PaperTradeDetail"] & {
  book?: string;
  sleeve?: string | null;
  lane?: string | null;
  structure?: string | null;
  instrument_kind?: string | null;
  initial_risk?: number | null;
  execution?: Record<string, unknown>;
  entry_price?: number | null;
  exit_price?: number | null;
  staged_limit_price?: number | null;
  filled_quantity?: number | null;
  remaining_quantity?: number | null;
  realized_pnl?: number | null;
  unrealized_pnl?: number | null;
  net_pnl?: number | null;
  mark_status?: string;
  mark_price?: number | null;
  mark_value?: number | null;
  mark_observed_at?: string | null;
  mark_available_at?: string | null;
  mark_source?: string | null;
  mark_basis?: string | null;
  mark_stale?: boolean | null;
  mark?: Record<string, unknown>;
  strategy?: { revision?: number | null; name?: string | null; model_revision?: string | null };
  decision_at?: string | null;
  staged_at?: string | null;
  evidence_reasons?: string[];
};

export type PaperPerformance = ApiSchema["PaperBookPerformance"] & {
  counts: Record<string, number>;
  net_pnl: number | null;
  realized_pnl: number | null;
  unrealized_pnl: number | null;
  open_exposure?: number | null;
  open_exposure_status?: string;
  nav?: number | null;
  nav_status?: string;
  return_pct?: number | null;
  return_status?: string;
  capital_status?: string;
  flow_status?: string;
  drawdown?: number | null;
  quality_status: string;
  missing_evidence_reasons: string[];
  evidence_coverage?: { realized_pnl_coverage?: number | null; mark_coverage?: number | null; reconciled_orders?: number };
  accounting_basis?: string;
  series?: { points?: Array<{ at: string; cumulative_net_pnl: number; trade_id: string }>; drawdown_points?: Array<{ at: string; drawdown: number; trade_id: string }>; available_series?: string[]; gaps?: Array<{ reason: string; trade_id?: string }>; drawdown_basis?: string };
};

export type PaperTradePagePayload = ApiSchema["PaperTradePage"] & {
  rows: PaperTrade[];
  next_cursor: string | null;
};

export type LearningOverviewPayload = ApiSchema["LearningOverview"] & {
  paper_only: boolean;
  strategy_lane: Record<string, any>;
  prediction_lane: Record<string, any>;
  diagnostics: ApiSchema["ResearchDiagnostics"];
  paper: PaperPerformance;
  events?: Array<Record<string, unknown>>;
};

export type PaperFilters = {
  book?: string;
  sleeve?: string;
  symbol?: string;
  instrument_kind?: string;
  strategy_revision?: string;
  lifecycle?: string;
  date_from?: string;
  date_to?: string;
  lane?: string;
  structure?: string;
  evidence_class?: string;
  reconciliation_status?: string;
};

function query(filters: PaperFilters): string {
  const params = new URLSearchParams();
  if (filters.book) params.set("book", filters.book);
  if (filters.sleeve) params.set("sleeve", filters.sleeve);
  if (filters.symbol) params.set("symbol", filters.symbol);
  if (filters.instrument_kind) params.set("instrument_kind", filters.instrument_kind);
  if (filters.strategy_revision) params.set("strategy_revision", filters.strategy_revision);
  if (filters.lifecycle) params.set("lifecycle", filters.lifecycle);
  if (filters.date_from) params.set("date_from", filters.date_from);
  if (filters.date_to) params.set("date_to", filters.date_to);
  if (filters.lane) params.set("lane", filters.lane);
  if (filters.structure) params.set("structure", filters.structure);
  if (filters.evidence_class) params.set("evidence_class", filters.evidence_class);
  if (filters.reconciliation_status) params.set("reconciliation_status", filters.reconciliation_status);
  return params.toString();
}

export function loadPaperPerformance(filters: PaperFilters, signal?: AbortSignal): Promise<PaperPerformance> {
  const suffix = query(filters);
  return getJson<PaperPerformance>(`/api/paper/performance${suffix ? `?${suffix}` : ""}`, signal);
}

export function loadPaperTrades(filters: PaperFilters, limit = 100, cursor?: string | null, signal?: AbortSignal): Promise<PaperTradePagePayload> {
  const params = new URLSearchParams(query(filters));
  params.set("limit", String(limit));
  if (cursor) params.set("cursor", cursor);
  return getJson<PaperTradePagePayload>(`/api/paper/trades?${params.toString()}`, signal);
}

export function loadPaperTrade(tradeId: string, signal?: AbortSignal): Promise<PaperTrade> {
  return getJson<PaperTrade>(`/api/paper/trades/${encodeURIComponent(tradeId)}`, signal);
}

export function paperTradesExportUrl(filters: PaperFilters): string {
  const params = new URLSearchParams(query(filters));
  params.set("format", "csv");
  return `/api/paper/trades/export?${params.toString()}`;
}

export function loadLearningOverview(signal?: AbortSignal): Promise<LearningOverviewPayload> {
  return getJson<LearningOverviewPayload>("/api/research/overview", signal);
}
