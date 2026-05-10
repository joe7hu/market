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
  found?: boolean;
};

export type SettingsPayload = {
  status?: ApiStatus;
  config?: Record<string, JsonValue>;
  integration?: Record<string, JsonValue>;
};

export type PanelData = {
  dashboard: DashboardPayload;
  signals: TablePayload;
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
  news: TablePayload;
  sepa: TablePayload;
  liquidity: TablePayload;
  correlations: TablePayload;
  etfPremiums: TablePayload;
  analystEstimates: TablePayload;
  earnings: TablePayload;
  valuations: TablePayload;
  providerRuns: TablePayload;
  sourceHealth: TablePayload;
  settings: SettingsPayload;
  errors: Partial<Record<PanelEndpoint, string>>;
};

export type PanelEndpoint =
  | "dashboard"
  | "signals"
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
  | "news"
  | "sepa"
  | "liquidity"
  | "correlations"
  | "etfPremiums"
  | "analystEstimates"
  | "earnings"
  | "valuations"
  | "providerRuns"
  | "sourceHealth"
  | "settings";
