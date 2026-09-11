import type { components } from "../generated/apiSchema";
import { getJson } from "../apiTransport";

type ApiSchema = components["schemas"];

export type PaperTrade = ApiSchema["PaperTradeDetail"] & {
  entry_price?: number | null;
  exit_price?: number | null;
  staged_limit_price?: number | null;
  filled_quantity?: number | null;
  remaining_quantity?: number | null;
  realized_pnl?: number | null;
  unrealized_pnl?: number | null;
  net_pnl?: number | null;
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
  quality_status: string;
  missing_evidence_reasons: string[];
  evidence_coverage?: { realized_pnl_coverage?: number | null; mark_coverage?: number | null; reconciled_orders?: number };
  accounting_basis?: string;
  series?: { points?: Array<{ at: string; cumulative_net_pnl: number; trade_id: string }>; gaps?: Array<{ reason: string }> };
};

export type PaperTradePagePayload = ApiSchema["PaperTradePage"] & {
  rows: PaperTrade[];
  next_cursor: string | null;
};

export type LearningOverviewPayload = ApiSchema["LearningOverview"] & {
  paper_only: boolean;
  strategy_lane: Record<string, any>;
  prediction_lane: Record<string, any>;
  paper: PaperPerformance;
  events?: Array<Record<string, unknown>>;
};

export type PaperFilters = { symbol?: string; strategy_revision?: string; lifecycle?: string };

function query(filters: PaperFilters): string {
  const params = new URLSearchParams();
  if (filters.symbol) params.set("symbol", filters.symbol);
  if (filters.strategy_revision) params.set("strategy_revision", filters.strategy_revision);
  if (filters.lifecycle) params.set("lifecycle", filters.lifecycle);
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

export function loadLearningOverview(signal?: AbortSignal): Promise<LearningOverviewPayload> {
  return getJson<LearningOverviewPayload>("/api/research/overview", signal);
}
