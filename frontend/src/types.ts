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
  thesisMonitor: TablePayload;
  traderTwins: TablePayload;
  catalysts: TablePayload;
  fundamentals: TablePayload;
  disclosures: TablePayload;
  quotes: TablePayload;
  screener: TablePayload;
  optionsExpiries: TablePayload;
  optionsChain: TablePayload;
  optionsPayoffScenarios: TablePayload;
  optionsProviderCapabilities: TablePayload;
  optionsExpirySignals: TablePayload;
  optionsTickerSignals: TablePayload;
  optionStrategyVersions: TablePayload;
  optionRadarSummary: TablePayload;
  optionRadarOpportunity: TablePayload;
  optionSnapshot: TablePayload;
  optionFeatures: TablePayload;
  stockFeatures: TablePayload;
  agentThesis: TablePayload;
  agentThesisRequest: TablePayload;
  agentThesisValidation: TablePayload;
  agentPostmortemRequest: TablePayload;
  agentPostmortem: TablePayload;
  candidateEvent: TablePayload;
  candidateEventMark: TablePayload;
  candidateEventAttribution: TablePayload;
  shadowTrade: TablePayload;
  shadowTradeMark: TablePayload;
  radarStateTransition: TablePayload;
  radarAlert: TablePayload;
  convictionCalibration: TablePayload;
  volSurfaceFeatures: TablePayload;
  tradeJournal: TablePayload;
  optionAttribution: TablePayload;
  missedWinnerEvent: TablePayload;
  strategyMutationProposal: TablePayload;
  strategyBacktestResult: TablePayload;
  strategyForwardTestResult: TablePayload;
  strategyCohortResult: TablePayload;
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
  brokerStatus: TablePayload;
  brokerAccounts: TablePayload;
  brokerPositions: TablePayload;
  brokerMarketSnapshots: TablePayload;
  brokerScannerSignals: TablePayload;
  agentRecommendations: TablePayload;
  paperOrders: TablePayload;
  dailyBrief: TablePayload;
  feedSignals: TablePayload;
  universeScreen: TablePayload;
  watchlistWatched: TablePayload;
  watchlistUnwatched: TablePayload;
  watchlistWatchedQuotes: TablePayload;
  watchlistUnwatchedQuotes: TablePayload;
  watchlistWatchedFundamentals: TablePayload;
  watchlistUnwatchedFundamentals: TablePayload;
  watchlistWatchedTechnicals: TablePayload;
  watchlistUnwatchedTechnicals: TablePayload;
  watchlistWatchedValuations: TablePayload;
  watchlistUnwatchedValuations: TablePayload;
  watchlistWatchedScreener: TablePayload;
  watchlistUnwatchedScreener: TablePayload;
  watchlistWatchedDecisionQueue: TablePayload;
  watchlistUnwatchedDecisionQueue: TablePayload;
  watchlistWatchedPortfolio: TablePayload;
  watchlistUnwatchedPortfolio: TablePayload;
  watchlistWatchedOptions: TablePayload;
  watchlistUnwatchedOptions: TablePayload;
  manualWatchlist: TablePayload;
  sources: TablePayload;
  sourceConsensus: TablePayload;
  sourceTickerRankings: TablePayload;
  sourceItems: TablePayload;
  tickerSourceSignals: TablePayload;
  ownershipConsensus: TablePayload;
  marketContext: TablePayload;
  marketValuationReferenceCharts: TablePayload;
  marketValuationCharts: TablePayload;
  marketEnvironmentAssets: TablePayload;
  marketEnvironmentModel: TablePayload;
  exposureClusters: TablePayload;
  correlationEdges: TablePayload;
  portfolioRiskCards: TablePayload;
  reviewActions: TablePayload;
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
  | "thesisMonitor"
  | "traderTwins"
  | "catalysts"
  | "fundamentals"
  | "disclosures"
  | "quotes"
  | "screener"
  | "optionsExpiries"
  | "optionsChain"
  | "optionsPayoffScenarios"
  | "optionsProviderCapabilities"
  | "optionsExpirySignals"
  | "optionsTickerSignals"
  | "optionStrategyVersions"
  | "optionRadarSummary"
  | "optionRadarOpportunity"
  | "optionSnapshot"
  | "optionFeatures"
  | "stockFeatures"
  | "agentThesis"
  | "agentThesisRequest"
  | "agentThesisValidation"
  | "agentPostmortemRequest"
  | "agentPostmortem"
  | "candidateEvent"
  | "candidateEventMark"
  | "candidateEventAttribution"
  | "shadowTrade"
  | "shadowTradeMark"
  | "radarStateTransition"
  | "radarAlert"
  | "convictionCalibration"
  | "volSurfaceFeatures"
  | "tradeJournal"
  | "optionAttribution"
  | "missedWinnerEvent"
  | "strategyMutationProposal"
  | "strategyBacktestResult"
  | "strategyForwardTestResult"
  | "strategyCohortResult"
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
  | "brokerStatus"
  | "brokerAccounts"
  | "brokerPositions"
  | "brokerMarketSnapshots"
  | "brokerScannerSignals"
  | "agentRecommendations"
  | "paperOrders"
  | "dailyBrief"
  | "feedSignals"
  | "universeScreen"
  | "watchlistWatched"
  | "watchlistUnwatched"
  | "watchlistWatchedQuotes"
  | "watchlistUnwatchedQuotes"
  | "watchlistWatchedFundamentals"
  | "watchlistUnwatchedFundamentals"
  | "watchlistWatchedTechnicals"
  | "watchlistUnwatchedTechnicals"
  | "watchlistWatchedValuations"
  | "watchlistUnwatchedValuations"
  | "watchlistWatchedScreener"
  | "watchlistUnwatchedScreener"
  | "watchlistWatchedDecisionQueue"
  | "watchlistUnwatchedDecisionQueue"
  | "watchlistWatchedPortfolio"
  | "watchlistUnwatchedPortfolio"
  | "watchlistWatchedOptions"
  | "watchlistUnwatchedOptions"
  | "manualWatchlist"
  | "sources"
  | "sourceConsensus"
  | "sourceTickerRankings"
  | "sourceItems"
  | "tickerSourceSignals"
  | "ownershipConsensus"
  | "marketContext"
  | "marketValuationReferenceCharts"
  | "marketValuationCharts"
  | "marketEnvironmentAssets"
  | "marketEnvironmentModel"
  | "exposureClusters"
  | "correlationEdges"
  | "portfolioRiskCards"
  | "reviewActions"
  | "sourceHealth"
  | "refreshJobs"
  | "settings";
