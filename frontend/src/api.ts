import type {
  DashboardPayload,
  PanelData,
  PanelEndpoint,
  RowRecord,
  SettingsPayload,
  TablePayload,
  TickerPayload,
} from "./types";

const EMPTY_TABLE: TablePayload = { rows: [], count: 0 };

type PanelSnapshotPayload = {
  scope?: string;
  status?: DashboardPayload["status"];
  dashboard?: DashboardPayload | null;
  tables?: Record<string, TablePayload>;
};

const TABLE_KEYS: Record<string, keyof PanelData> = {
  discovered_universe: "discoveredUniverse",
  decision_queue: "decisionQueue",
  decision_readiness: "decisionReadiness",
  source_freshness: "sourceFreshness",
  symbol_decision_snapshots: "symbolDecisionSnapshots",
  signals: "signals",
  opportunities_ranked: "opportunitiesRanked",
  opportunity_sources: "opportunitySources",
  candidates: "candidates",
  portfolio: "portfolio",
  theses: "theses",
  trader_twins: "traderTwins",
  catalysts: "catalysts",
  fundamentals: "fundamentals",
  disclosures: "disclosures",
  quotes: "quotes",
  screener: "screener",
  options_expiries: "optionsExpiries",
  options_chain: "optionsChain",
  options_payoff_scenarios: "optionsPayoffScenarios",
  news: "news",
  tradingview_symbol_search: "tradingviewSymbolSearch",
  tradingview_watchlists: "tradingviewWatchlists",
  tradingview_alerts: "tradingviewAlerts",
  tradingview_chart_state: "tradingviewChartState",
  sepa: "sepa",
  liquidity: "liquidity",
  correlations: "correlations",
  etf_premiums: "etfPremiums",
  analyst_estimates: "analystEstimates",
  earnings: "earnings",
  earnings_setups: "earningsSetups",
  valuations: "valuations",
  technicals: "technicals",
  research_packets: "researchPackets",
  ticker_memos: "memos",
  provider_runs: "providerRuns",
  broker_status: "brokerStatus",
  broker_accounts: "brokerAccounts",
  broker_positions: "brokerPositions",
  broker_market_snapshots: "brokerMarketSnapshots",
  broker_scanner_signals: "brokerScannerSignals",
  agent_recommendations: "agentRecommendations",
  paper_orders: "paperOrders",
  source_health: "sourceHealth",
  refresh_jobs: "refreshJobs",
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

async function sendJson<T>(path: string, method: "POST" | "DELETE", body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    cache: "no-store",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

async function settle<T>(
  endpoint: PanelEndpoint,
  request: Promise<T>,
): Promise<{ endpoint: PanelEndpoint; value?: T; error?: string }> {
  try {
    return { endpoint, value: await request };
  } catch (error) {
    return {
      endpoint,
      error: error instanceof Error ? error.message : "Request failed",
    };
  }
}

export async function loadPanelData(): Promise<PanelData> {
  return loadPanelScope("dashboard");
}

export async function loadPanelScope(scope: string, existing?: PanelData): Promise<PanelData> {
  const snapshot = await getJson<PanelSnapshotPayload>(`/api/panel-snapshot?scope=${encodeURIComponent(scope)}`);
  const data = mergeSnapshot(existing ?? emptyPanelData(), snapshot);
  if (scope === "settings") {
    data.settings = await getJson<SettingsPayload>("/api/settings");
  }
  return data;
}

export function emptyPanelData(): PanelData {
  return {
    dashboard: {},
    discoveredUniverse: EMPTY_TABLE,
    decisionQueue: EMPTY_TABLE,
    decisionReadiness: EMPTY_TABLE,
    sourceFreshness: EMPTY_TABLE,
    symbolDecisionSnapshots: EMPTY_TABLE,
    signals: EMPTY_TABLE,
    opportunitiesRanked: EMPTY_TABLE,
    opportunitySources: EMPTY_TABLE,
    candidates: EMPTY_TABLE,
    portfolio: EMPTY_TABLE,
    theses: EMPTY_TABLE,
    traderTwins: EMPTY_TABLE,
    catalysts: EMPTY_TABLE,
    fundamentals: EMPTY_TABLE,
    disclosures: EMPTY_TABLE,
    quotes: EMPTY_TABLE,
    screener: EMPTY_TABLE,
    optionsExpiries: EMPTY_TABLE,
    optionsChain: EMPTY_TABLE,
    optionsPayoffScenarios: EMPTY_TABLE,
    news: EMPTY_TABLE,
    tradingviewSymbolSearch: EMPTY_TABLE,
    tradingviewWatchlists: EMPTY_TABLE,
    tradingviewAlerts: EMPTY_TABLE,
    tradingviewChartState: EMPTY_TABLE,
    sepa: EMPTY_TABLE,
    liquidity: EMPTY_TABLE,
    correlations: EMPTY_TABLE,
    etfPremiums: EMPTY_TABLE,
    analystEstimates: EMPTY_TABLE,
    earnings: EMPTY_TABLE,
    earningsSetups: EMPTY_TABLE,
    valuations: EMPTY_TABLE,
    technicals: EMPTY_TABLE,
    researchPackets: EMPTY_TABLE,
    memos: EMPTY_TABLE,
    providerRuns: EMPTY_TABLE,
    brokerStatus: EMPTY_TABLE,
    brokerAccounts: EMPTY_TABLE,
    brokerPositions: EMPTY_TABLE,
    brokerMarketSnapshots: EMPTY_TABLE,
    brokerScannerSignals: EMPTY_TABLE,
    agentRecommendations: EMPTY_TABLE,
    paperOrders: EMPTY_TABLE,
    sourceHealth: EMPTY_TABLE,
    refreshJobs: EMPTY_TABLE,
    settings: {},
    errors: {},
  };
}

function mergeSnapshot(existing: PanelData, snapshot: PanelSnapshotPayload): PanelData {
  const next: PanelData = { ...existing, errors: { ...existing.errors } };
  if (snapshot.dashboard) {
    next.dashboard = snapshot.dashboard;
  }
  for (const [apiKey, table] of Object.entries(snapshot.tables ?? {})) {
    const dataKey = TABLE_KEYS[apiKey];
    if (dataKey && dataKey !== "dashboard" && dataKey !== "settings" && dataKey !== "errors") {
      (next[dataKey] as TablePayload) = table ?? EMPTY_TABLE;
    }
  }
  return next;
}

export async function loadLegacyPanelData(): Promise<PanelData> {
  const results = await Promise.all([
    settle("dashboard", getJson<DashboardPayload>("/api/dashboard")),
    settle("discoveredUniverse", getJson<TablePayload>("/api/discovered-universe")),
    settle("decisionQueue", getJson<TablePayload>("/api/decision-queue")),
    settle("sourceFreshness", getJson<TablePayload>("/api/source-freshness")),
    settle("symbolDecisionSnapshots", getJson<TablePayload>("/api/symbol-decision-snapshots")),
    settle("signals", getJson<TablePayload>("/api/signals")),
    settle("opportunitiesRanked", getJson<TablePayload>("/api/opportunities-ranked")),
    settle("opportunitySources", getJson<TablePayload>("/api/opportunity-sources")),
    settle("candidates", getJson<TablePayload>("/api/candidates")),
    settle("portfolio", getJson<TablePayload>("/api/portfolio")),
    settle("theses", getJson<TablePayload>("/api/theses")),
    settle("traderTwins", getJson<TablePayload>("/api/trader-twins")),
    settle("catalysts", getJson<TablePayload>("/api/catalysts")),
    settle("fundamentals", getJson<TablePayload>("/api/fundamentals")),
    settle("disclosures", getJson<TablePayload>("/api/disclosures")),
    settle("quotes", getJson<TablePayload>("/api/quotes")),
    settle("screener", getJson<TablePayload>("/api/screener")),
    settle("optionsExpiries", getJson<TablePayload>("/api/options-expiries")),
    settle("optionsChain", getJson<TablePayload>("/api/options-chain")),
    settle("optionsPayoffScenarios", getJson<TablePayload>("/api/options-payoff-scenarios")),
    settle("news", getJson<TablePayload>("/api/news")),
    settle("tradingviewSymbolSearch", getJson<TablePayload>("/api/tradingview-symbol-search")),
    settle("tradingviewWatchlists", getJson<TablePayload>("/api/tradingview-watchlists")),
    settle("tradingviewAlerts", getJson<TablePayload>("/api/tradingview-alerts")),
    settle("tradingviewChartState", getJson<TablePayload>("/api/tradingview-chart-state")),
    settle("sepa", getJson<TablePayload>("/api/sepa")),
    settle("liquidity", getJson<TablePayload>("/api/liquidity")),
    settle("correlations", getJson<TablePayload>("/api/correlations")),
    settle("etfPremiums", getJson<TablePayload>("/api/etf-premiums")),
    settle("analystEstimates", getJson<TablePayload>("/api/analyst-estimates")),
    settle("earnings", getJson<TablePayload>("/api/earnings")),
    settle("earningsSetups", getJson<TablePayload>("/api/earnings-setups")),
    settle("valuations", getJson<TablePayload>("/api/valuations")),
    settle("technicals", getJson<TablePayload>("/api/technicals")),
    settle("researchPackets", getJson<TablePayload>("/api/research-packets")),
    settle("memos", getJson<TablePayload>("/api/memos")),
    settle("providerRuns", getJson<TablePayload>("/api/provider-runs")),
    settle("brokerStatus", getJson<TablePayload>("/api/broker/status")),
    settle("brokerAccounts", getJson<TablePayload>("/api/broker/accounts")),
    settle("brokerPositions", getJson<TablePayload>("/api/broker/positions")),
    settle("agentRecommendations", getJson<TablePayload>("/api/agent/recommendations")),
    settle("paperOrders", getJson<TablePayload>("/api/paper-orders")),
    settle("sourceHealth", getJson<TablePayload>("/api/source-health")),
    settle("settings", getJson<SettingsPayload>("/api/settings")),
  ]);

  const errors: PanelData["errors"] = {};
  const data: PanelData = {
    dashboard: {},
    discoveredUniverse: EMPTY_TABLE,
    decisionQueue: EMPTY_TABLE,
    decisionReadiness: EMPTY_TABLE,
    sourceFreshness: EMPTY_TABLE,
    symbolDecisionSnapshots: EMPTY_TABLE,
    signals: EMPTY_TABLE,
    opportunitiesRanked: EMPTY_TABLE,
    opportunitySources: EMPTY_TABLE,
    candidates: EMPTY_TABLE,
    portfolio: EMPTY_TABLE,
    theses: EMPTY_TABLE,
    traderTwins: EMPTY_TABLE,
    catalysts: EMPTY_TABLE,
    fundamentals: EMPTY_TABLE,
    disclosures: EMPTY_TABLE,
    quotes: EMPTY_TABLE,
    screener: EMPTY_TABLE,
    optionsExpiries: EMPTY_TABLE,
    optionsChain: EMPTY_TABLE,
    optionsPayoffScenarios: EMPTY_TABLE,
    news: EMPTY_TABLE,
    tradingviewSymbolSearch: EMPTY_TABLE,
    tradingviewWatchlists: EMPTY_TABLE,
    tradingviewAlerts: EMPTY_TABLE,
    tradingviewChartState: EMPTY_TABLE,
    sepa: EMPTY_TABLE,
    liquidity: EMPTY_TABLE,
    correlations: EMPTY_TABLE,
    etfPremiums: EMPTY_TABLE,
    analystEstimates: EMPTY_TABLE,
    earnings: EMPTY_TABLE,
    earningsSetups: EMPTY_TABLE,
    valuations: EMPTY_TABLE,
    technicals: EMPTY_TABLE,
    researchPackets: EMPTY_TABLE,
    memos: EMPTY_TABLE,
    providerRuns: EMPTY_TABLE,
    brokerStatus: EMPTY_TABLE,
    brokerAccounts: EMPTY_TABLE,
    brokerPositions: EMPTY_TABLE,
    brokerMarketSnapshots: EMPTY_TABLE,
    brokerScannerSignals: EMPTY_TABLE,
    agentRecommendations: EMPTY_TABLE,
    paperOrders: EMPTY_TABLE,
    sourceHealth: EMPTY_TABLE,
    refreshJobs: EMPTY_TABLE,
    settings: {},
    errors,
  };

  for (const result of results) {
    if (result.error) {
      errors[result.endpoint] = result.error;
      continue;
    }
    switch (result.endpoint) {
      case "dashboard":
        data.dashboard = (result.value as DashboardPayload) ?? {};
        break;
      case "discoveredUniverse":
        data.discoveredUniverse = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "decisionQueue":
        data.decisionQueue = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "sourceFreshness":
        data.sourceFreshness = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "symbolDecisionSnapshots":
        data.symbolDecisionSnapshots = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "signals":
        data.signals = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "opportunitiesRanked":
        data.opportunitiesRanked = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "opportunitySources":
        data.opportunitySources = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "candidates":
        data.candidates = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "portfolio":
        data.portfolio = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "theses":
        data.theses = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "traderTwins":
        data.traderTwins = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "catalysts":
        data.catalysts = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "fundamentals":
        data.fundamentals = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "disclosures":
        data.disclosures = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "quotes":
        data.quotes = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "screener":
        data.screener = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "optionsExpiries":
        data.optionsExpiries = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "optionsChain":
        data.optionsChain = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "optionsPayoffScenarios":
        data.optionsPayoffScenarios = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "news":
        data.news = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "tradingviewSymbolSearch":
        data.tradingviewSymbolSearch = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "tradingviewWatchlists":
        data.tradingviewWatchlists = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "tradingviewAlerts":
        data.tradingviewAlerts = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "tradingviewChartState":
        data.tradingviewChartState = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "sepa":
        data.sepa = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "liquidity":
        data.liquidity = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "correlations":
        data.correlations = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "etfPremiums":
        data.etfPremiums = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "analystEstimates":
        data.analystEstimates = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "earnings":
        data.earnings = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "earningsSetups":
        data.earningsSetups = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "valuations":
        data.valuations = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "technicals":
        data.technicals = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "researchPackets":
        data.researchPackets = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "memos":
        data.memos = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "providerRuns":
        data.providerRuns = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "brokerStatus":
        data.brokerStatus = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "brokerAccounts":
        data.brokerAccounts = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "brokerPositions":
        data.brokerPositions = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "agentRecommendations":
        data.agentRecommendations = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "paperOrders":
        data.paperOrders = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "sourceHealth":
        data.sourceHealth = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "settings":
        data.settings = (result.value as SettingsPayload) ?? {};
        break;
    }
  }

  return data;
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

export async function runAgentReview(): Promise<TablePayload> {
  const payload = await sendJson<TablePayload>("/api/agent/review", "POST");
  return payload;
}

export async function stagePaperOrder(recommendationId: string): Promise<RowRecord> {
  return sendJson<RowRecord>("/api/paper-orders", "POST", { recommendation_id: recommendationId });
}
