import type {
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

// --- Source catalog (GET /api/source-catalog) ------------------------------
// Backend tones are good|warn|bad|neutral|unknown; the UI Tone system uses
// good|warn|bad|info|muted, so the catalog views map neutral/unknown -> muted.
export type SourceCatalogTone = "good" | "warn" | "bad" | "neutral" | "unknown";

export type SourceCatalogProvider = {
  provider: string;
  status: "ok" | "stale" | "failed" | "rate_limited" | "unknown" | string;
  tone: SourceCatalogTone;
  provider_status: "ok" | "degraded" | "failed" | string;
  last_observed_at: string | null;
  stale_after: string;
  symbol_count: number;
  rate_limited: boolean;
  freshness_status: "fresh" | "stale" | string;
  detail: string;
};

export type SourceCatalogCategory = {
  id: string;
  label: string;
  family: string;
  cadence_label: string;
  cadence_seconds: number;
  refresh_job: string;
  stale_after: string;
  source_types: string[];
  live_fetcher: boolean;
  tone: SourceCatalogTone;
  primary: SourceCatalogProvider | null;
  fallback: SourceCatalogProvider[];
};

export type SourceCatalogPayload = {
  categories: SourceCatalogCategory[];
  families: Record<string, string[]>;
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
  command?: string;
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
};

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${path}${path.includes("?") ? "&" : "?"}_=${Date.now()}`, {
    cache: "no-store",
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

export type AgentOverview = {
  config: Record<string, unknown>;
  pricing: Record<string, { input_per_1m?: number; output_per_1m?: number }>;
  queue: { thesis_open: number; postmortem_open: number; total_open: number; oldest_open_at?: string | null };
  runs: AgentRun[];
  cost: { today: AgentCostWindow; last_7d: AgentCostWindow };
  scheduler: { agent_refresh_seconds: number };
};

export async function loadAgent(): Promise<AgentOverview> {
  return getJson<AgentOverview>("/api/agent");
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

export type PortfolioPositionInput = {
  symbol: string;
  quantity: number;
  avg_cost: number;
  purchase_date?: string;
  notes?: string;
};

export async function savePortfolioPosition(position: PortfolioPositionInput): Promise<TablePayload> {
  const payload = await sendJson<{ portfolio: TablePayload }>("/api/portfolio/positions", "POST", position);
  return payload.portfolio;
}

export async function deletePortfolioPosition(symbol: string): Promise<TablePayload> {
  const payload = await sendJson<{ portfolio: TablePayload }>(`/api/portfolio/positions/${encodeURIComponent(symbol)}`, "DELETE");
  return payload.portfolio;
}

export type ThesisInput = {
  thesis: string;
  why?: string;
  invalidation?: string;
  invalidation_price?: number | null;
  status?: string | null;
  evidence_links?: string[];
};

export async function saveThesis(symbol: string, input: ThesisInput): Promise<void> {
  await sendJson<{ thesis: unknown }>(`/api/theses/${encodeURIComponent(symbol)}`, "PUT", input);
}

export async function markThesisReviewed(symbol: string): Promise<void> {
  await sendJson<{ review: unknown }>(`/api/theses/${encodeURIComponent(symbol)}/review`, "POST");
}

export async function runAgentReview(): Promise<TablePayload> {
  const payload = await sendJson<TablePayload>("/api/agent/review", "POST");
  return payload;
}

export async function stagePaperOrder(recommendationId: string): Promise<RowRecord> {
  return sendJson<RowRecord>("/api/paper-orders", "POST", { recommendation_id: recommendationId });
}
