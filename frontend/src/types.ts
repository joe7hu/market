export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

export type RowRecord = Record<string, JsonValue | undefined>;

export type ApiStatus = {
  ready?: boolean;
  message?: string;
  source?: string;
  metadata?: Record<string, JsonValue>;
};

export type TablePayload = {
  rows?: RowRecord[];
  count?: number;
  status?: ApiStatus;
};

export type DashboardPayload = {
  status?: ApiStatus;
  metrics?: Record<string, number>;
  priority_candidates?: RowRecord[];
  near_term_catalysts?: RowRecord[];
  portfolio?: RowRecord[];
  news?: RowRecord[];
};

export type TickerPayload = {
  ticker?: string;
  status?: ApiStatus;
  tables?: Record<string, RowRecord[] | undefined>;
  decision_snapshot?: RowRecord;
  decision_brief?: RowRecord;
  found?: boolean;
};

export type SettingsPayload = {
  status?: ApiStatus;
  config?: Record<string, JsonValue>;
  integration?: Record<string, JsonValue>;
};

export type PanelData = {
  dashboard: DashboardPayload;
  discoveredUniverse: TablePayload;
  decisionQueue: TablePayload;
  decisionReadiness: TablePayload;
  sourceFreshness: TablePayload;
  symbolDecisionSnapshots: TablePayload;
  signals: TablePayload;
  opportunitiesRanked: TablePayload;
  opportunitySources: TablePayload;
  candidates: TablePayload;
  portfolio: TablePayload;
  theses: TablePayload;
  traderTwins: TablePayload;
  catalysts: TablePayload;
  fundamentals: TablePayload;
  disclosures: TablePayload;
  quotes: TablePayload;
  screener: TablePayload;
  optionsExpiries: TablePayload;
  optionsChain: TablePayload;
  optionsPayoffScenarios: TablePayload;
  news: TablePayload;
  tradingviewSymbolSearch: TablePayload;
  tradingviewWatchlists: TablePayload;
  tradingviewAlerts: TablePayload;
  tradingviewChartState: TablePayload;
  sepa: TablePayload;
  liquidity: TablePayload;
  correlations: TablePayload;
  etfPremiums: TablePayload;
  analystEstimates: TablePayload;
  earnings: TablePayload;
  earningsSetups: TablePayload;
  valuations: TablePayload;
  technicals: TablePayload;
  researchPackets: TablePayload;
  memos: TablePayload;
  providerRuns: TablePayload;
  sourceHealth: TablePayload;
  refreshJobs: TablePayload;
  settings: SettingsPayload;
  errors: Partial<Record<PanelEndpoint, string>>;
};

export type PanelEndpoint =
  | "dashboard"
  | "discoveredUniverse"
  | "decisionQueue"
  | "decisionReadiness"
  | "sourceFreshness"
  | "symbolDecisionSnapshots"
  | "signals"
  | "opportunitiesRanked"
  | "opportunitySources"
  | "candidates"
  | "portfolio"
  | "theses"
  | "traderTwins"
  | "catalysts"
  | "fundamentals"
  | "disclosures"
  | "quotes"
  | "screener"
  | "optionsExpiries"
  | "optionsChain"
  | "optionsPayoffScenarios"
  | "news"
  | "tradingviewSymbolSearch"
  | "tradingviewWatchlists"
  | "tradingviewAlerts"
  | "tradingviewChartState"
  | "sepa"
  | "liquidity"
  | "correlations"
  | "etfPremiums"
  | "analystEstimates"
  | "earnings"
  | "earningsSetups"
  | "valuations"
  | "technicals"
  | "researchPackets"
  | "memos"
  | "providerRuns"
  | "sourceHealth"
  | "refreshJobs"
  | "settings";
