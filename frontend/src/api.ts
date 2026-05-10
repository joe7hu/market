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
