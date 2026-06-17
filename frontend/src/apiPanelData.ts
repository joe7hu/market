import type { DashboardPayload, PanelData, RowRecord, TablePayload } from "./types";

export const EMPTY_TABLE: TablePayload = { rows: [], count: 0 };

export type PanelSnapshotPayload = {
  scope?: string;
  status?: DashboardPayload["status"];
  dashboard?: DashboardPayload | null;
  tables?: Record<string, TablePayload>;
};

const TABLE_KEY_OVERRIDES: Record<string, keyof PanelData> = {
  ticker_memos: "memos",
};

let panelDataKeys: Set<string> | undefined;

function tableKeyFor(apiKey: string): keyof PanelData | undefined {
  if (apiKey in TABLE_KEY_OVERRIDES) return TABLE_KEY_OVERRIDES[apiKey];
  const key = apiKey.replace(/_([a-z0-9])/g, (_, letter: string) => letter.toUpperCase()) as keyof PanelData;
  panelDataKeys ??= new Set(Object.keys(emptyPanelData()));
  return panelDataKeys.has(key) ? key : undefined;
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
    thesisMonitor: EMPTY_TABLE,
    traderTwins: EMPTY_TABLE,
    catalysts: EMPTY_TABLE,
    fundamentals: EMPTY_TABLE,
    disclosures: EMPTY_TABLE,
    quotes: EMPTY_TABLE,
    screener: EMPTY_TABLE,
    optionsExpiries: EMPTY_TABLE,
    optionsChain: EMPTY_TABLE,
    optionsPayoffScenarios: EMPTY_TABLE,
    optionsProviderCapabilities: EMPTY_TABLE,
    optionsExpirySignals: EMPTY_TABLE,
    optionsTickerSignals: EMPTY_TABLE,
    optionStrategyVersions: EMPTY_TABLE,
    optionRadarSummary: EMPTY_TABLE,
    optionRadarOpportunity: EMPTY_TABLE,
    radarAlert: EMPTY_TABLE,
    optionSnapshot: EMPTY_TABLE,
    optionFeatures: EMPTY_TABLE,
    stockFeatures: EMPTY_TABLE,
    agentThesis: EMPTY_TABLE,
    agentThesisRequest: EMPTY_TABLE,
    agentThesisValidation: EMPTY_TABLE,
    agentPostmortemRequest: EMPTY_TABLE,
    agentPostmortem: EMPTY_TABLE,
    candidateEvent: EMPTY_TABLE,
    candidateEventMark: EMPTY_TABLE,
    candidateEventAttribution: EMPTY_TABLE,
    shadowTrade: EMPTY_TABLE,
    shadowTradeMark: EMPTY_TABLE,
    radarStateTransition: EMPTY_TABLE,
    convictionCalibration: EMPTY_TABLE,
    volSurfaceFeatures: EMPTY_TABLE,
    tradeJournal: EMPTY_TABLE,
    optionAttribution: EMPTY_TABLE,
    missedWinnerEvent: EMPTY_TABLE,
    strategyMutationProposal: EMPTY_TABLE,
    strategyBacktestResult: EMPTY_TABLE,
    strategyForwardTestResult: EMPTY_TABLE,
    strategyCohortResult: EMPTY_TABLE,
    explorationGateReport: EMPTY_TABLE,
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
    dailyBrief: EMPTY_TABLE,
    feedSignals: EMPTY_TABLE,
    universeScreen: EMPTY_TABLE,
    watchlistWatched: EMPTY_TABLE,
    watchlistUnwatched: EMPTY_TABLE,
    watchlistWatchedQuotes: EMPTY_TABLE,
    watchlistUnwatchedQuotes: EMPTY_TABLE,
    watchlistWatchedFundamentals: EMPTY_TABLE,
    watchlistUnwatchedFundamentals: EMPTY_TABLE,
    watchlistWatchedTechnicals: EMPTY_TABLE,
    watchlistUnwatchedTechnicals: EMPTY_TABLE,
    watchlistWatchedValuations: EMPTY_TABLE,
    watchlistUnwatchedValuations: EMPTY_TABLE,
    watchlistWatchedScreener: EMPTY_TABLE,
    watchlistUnwatchedScreener: EMPTY_TABLE,
    watchlistWatchedDecisionQueue: EMPTY_TABLE,
    watchlistUnwatchedDecisionQueue: EMPTY_TABLE,
    watchlistWatchedResearchPackets: EMPTY_TABLE,
    watchlistUnwatchedResearchPackets: EMPTY_TABLE,
    watchlistWatchedMemos: EMPTY_TABLE,
    watchlistUnwatchedMemos: EMPTY_TABLE,
    watchlistWatchedThesisMonitor: EMPTY_TABLE,
    watchlistUnwatchedThesisMonitor: EMPTY_TABLE,
    watchlistWatchedPortfolio: EMPTY_TABLE,
    watchlistUnwatchedPortfolio: EMPTY_TABLE,
    watchlistWatchedOptions: EMPTY_TABLE,
    watchlistUnwatchedOptions: EMPTY_TABLE,
    manualWatchlist: EMPTY_TABLE,
    sources: EMPTY_TABLE,
    sourceConsensus: EMPTY_TABLE,
    sourceTickerRankings: EMPTY_TABLE,
    sourceItems: EMPTY_TABLE,
    tickerSourceSignals: EMPTY_TABLE,
    ownershipConsensus: EMPTY_TABLE,
    marketContext: EMPTY_TABLE,
    marketValuationReferenceCharts: EMPTY_TABLE,
    marketValuationCharts: EMPTY_TABLE,
    marketEnvironmentAssets: EMPTY_TABLE,
    marketEnvironmentModel: EMPTY_TABLE,
    exposureClusters: EMPTY_TABLE,
    correlationEdges: EMPTY_TABLE,
    portfolioRiskCards: EMPTY_TABLE,
    reviewActions: EMPTY_TABLE,
    sourceHealth: EMPTY_TABLE,
    sourceCatalog: EMPTY_TABLE,
    refreshJobs: EMPTY_TABLE,
    settings: {},
    errors: {},
  };
}

export function mergeSnapshot(existing: PanelData, snapshot: PanelSnapshotPayload, options: { append?: boolean } = {}): PanelData {
  const next: PanelData = { ...existing, errors: { ...existing.errors } };
  if (snapshot.dashboard) {
    next.dashboard = snapshot.dashboard;
  } else if (snapshot.status) {
    next.dashboard = { ...next.dashboard, status: snapshot.status };
  }
  for (const [apiKey, table] of Object.entries(snapshot.tables ?? {})) {
    const dataKey = tableKeyFor(apiKey);
    if (dataKey && dataKey !== "dashboard" && dataKey !== "settings" && dataKey !== "errors") {
      const existingTable = next[dataKey] as TablePayload;
      (next[dataKey] as TablePayload) = options.append ? appendTable(existingTable, table ?? EMPTY_TABLE) : table ?? EMPTY_TABLE;
    }
  }
  return next;
}

function appendTable(existing: TablePayload, incoming: TablePayload): TablePayload {
  const existingRows = existing.rows ?? [];
  const incomingRows = incoming.rows ?? [];
  return {
    ...incoming,
    rows: appendUniqueRows(existingRows, incomingRows),
    count: incoming.count ?? existing.count,
  };
}

function appendUniqueRows(existingRows: RowRecord[], incomingRows: RowRecord[]): RowRecord[] {
  const output = existingRows.slice();
  const seen = new Set(output.map(rowKey));
  for (const row of incomingRows) {
    const key = rowKey(row);
    if (seen.has(key)) continue;
    seen.add(key);
    output.push(row);
  }
  return output;
}

function rowKey(row: RowRecord): string {
  const symbol = String(row.symbol ?? row.ticker ?? "");
  const qualifier = String(row.method ?? row.source ?? row.source_key ?? row.id ?? row.date ?? row.as_of ?? "");
  return symbol || qualifier ? `${symbol}:${qualifier}` : JSON.stringify(row);
}
