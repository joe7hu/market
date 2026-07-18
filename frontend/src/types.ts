import type { PanelTableKey } from "./generated/panelContract";

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
  offset?: number;
  limit?: number | null;
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

export type Coverage = {
  status?: string;
  rows?: number;
  sources?: string[];
};

export type TickerDossier = {
  identity: RowRecord & { coverage?: Coverage };
  quote: RowRecord & { coverage?: Coverage };
  decision: RowRecord;
  fundamentals: { sec?: RowRecord; market?: RowRecord; coverage?: Coverage };
  estimates: {
    analyst?: { as_of?: string | null; earnings_estimate?: RowRecord[]; revenue_estimate?: RowRecord[]; price_targets?: RowRecord };
    earnings_event?: RowRecord;
    earnings_setup?: RowRecord;
    coverage?: Coverage;
  };
  technicals: { trend?: RowRecord; momentum?: RowRecord; sepa?: RowRecord; liquidity?: RowRecord; chart_context?: RowRecord; coverage?: Coverage };
  options: { signal?: RowRecord; unavailable_signals?: RowRecord[]; expiries?: RowRecord[]; capabilities?: RowRecord[]; context?: RowRecord; coverage?: Coverage };
  ownership: { institutional?: RowRecord; filings?: RowRecord[]; coverage?: Coverage };
  sources: { consensus?: RowRecord[]; signals?: RowRecord[]; evidence?: RowRecord[]; signal_count?: number; coverage?: Coverage };
  thesis: { state?: RowRecord; research_packet?: RowRecord; coverage?: Coverage };
  portfolio: {
    owned?: boolean;
    position?: RowRecord;
    fit?: RowRecord;
    risk_cards?: RowRecord[];
    exposure_clusters?: RowRecord[];
    correlations?: RowRecord[];
    review_actions?: RowRecord[];
    coverage?: Coverage;
  };
  coverage: {
    families?: Record<string, Coverage>;
    live?: string[];
    missing?: string[];
    loaded_families?: number;
    total_families?: number;
    as_of?: string | null;
  };
};

export type TickerPayload = {
  symbol?: string;
  ticker?: string;
  status?: ApiStatus;
  as_of?: string | null;
  dossier?: TickerDossier;
  found?: boolean;
};

export type SettingsPayload = {
  status?: ApiStatus;
  config?: Record<string, JsonValue>;
  sources?: TablePayload;
  agents?: {
    config?: Record<string, JsonValue>;
    runtime?: Record<string, JsonValue>;
    scheduler?: Record<string, JsonValue>;
    model_overrides?: Record<string, JsonValue>;
  };
  integration?: Record<string, JsonValue>;
};

export type KnownPanelTables = Record<PanelTableKey, TablePayload> & {
  refreshJobs: TablePayload;
};

export type PanelData = KnownPanelTables & {
  dashboard: DashboardPayload;
  settings: SettingsPayload;
  errors: Partial<Record<PanelEndpoint, string>>;
  [key: string]: DashboardPayload | SettingsPayload | TablePayload | Partial<Record<PanelEndpoint, string>> | undefined;
};

export type PanelEndpoint = keyof KnownPanelTables | "dashboard" | "settings";
