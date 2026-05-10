import type {
  DashboardPayload,
  PanelData,
  PanelEndpoint,
  SettingsPayload,
  TablePayload,
  TickerPayload,
} from "./types";

const EMPTY_TABLE: TablePayload = { rows: [], count: 0 };

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
  const results = await Promise.all([
    settle("dashboard", getJson<DashboardPayload>("/api/dashboard")),
    settle("signals", getJson<TablePayload>("/api/signals")),
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
    settle("news", getJson<TablePayload>("/api/news")),
    settle("sepa", getJson<TablePayload>("/api/sepa")),
    settle("liquidity", getJson<TablePayload>("/api/liquidity")),
    settle("correlations", getJson<TablePayload>("/api/correlations")),
    settle("etfPremiums", getJson<TablePayload>("/api/etf-premiums")),
    settle("analystEstimates", getJson<TablePayload>("/api/analyst-estimates")),
    settle("earnings", getJson<TablePayload>("/api/earnings")),
    settle("valuations", getJson<TablePayload>("/api/valuations")),
    settle("memos", getJson<TablePayload>("/api/memos")),
    settle("providerRuns", getJson<TablePayload>("/api/provider-runs")),
    settle("sourceHealth", getJson<TablePayload>("/api/source-health")),
    settle("settings", getJson<SettingsPayload>("/api/settings")),
  ]);

  const errors: PanelData["errors"] = {};
  const data: PanelData = {
    dashboard: {},
    signals: EMPTY_TABLE,
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
    news: EMPTY_TABLE,
    sepa: EMPTY_TABLE,
    liquidity: EMPTY_TABLE,
    correlations: EMPTY_TABLE,
    etfPremiums: EMPTY_TABLE,
    analystEstimates: EMPTY_TABLE,
    earnings: EMPTY_TABLE,
    valuations: EMPTY_TABLE,
    memos: EMPTY_TABLE,
    providerRuns: EMPTY_TABLE,
    sourceHealth: EMPTY_TABLE,
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
      case "signals":
        data.signals = (result.value as TablePayload) ?? EMPTY_TABLE;
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
      case "news":
        data.news = (result.value as TablePayload) ?? EMPTY_TABLE;
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
      case "valuations":
        data.valuations = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "memos":
        data.memos = (result.value as TablePayload) ?? EMPTY_TABLE;
        break;
      case "providerRuns":
        data.providerRuns = (result.value as TablePayload) ?? EMPTY_TABLE;
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
