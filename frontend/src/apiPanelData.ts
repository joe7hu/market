import type { DashboardPayload, PanelData, RowRecord, TablePayload } from "./types";

export const EMPTY_TABLE: TablePayload = { rows: [], count: 0 };

export type PanelSnapshotPayload = {
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
  thesis_monitor: "thesisMonitor",
  trader_twins: "traderTwins",
  catalysts: "catalysts",
  fundamentals: "fundamentals",
  disclosures: "disclosures",
  quotes: "quotes",
  screener: "screener",
  options_expiries: "optionsExpiries",
  options_chain: "optionsChain",
  options_payoff_scenarios: "optionsPayoffScenarios",
  options_provider_capabilities: "optionsProviderCapabilities",
  options_expiry_signals: "optionsExpirySignals",
  options_ticker_signals: "optionsTickerSignals",
  option_strategy_versions: "optionStrategyVersions",
  option_radar_summary: "optionRadarSummary",
  option_radar_opportunity: "optionRadarOpportunity",
  radar_alert: "radarAlert",
  option_snapshot: "optionSnapshot",
  option_features: "optionFeatures",
  stock_features: "stockFeatures",
  agent_thesis: "agentThesis",
  agent_thesis_request: "agentThesisRequest",
  agent_thesis_validation: "agentThesisValidation",
  agent_postmortem_request: "agentPostmortemRequest",
  agent_postmortem: "agentPostmortem",
  candidate_event: "candidateEvent",
  candidate_event_mark: "candidateEventMark",
  candidate_event_attribution: "candidateEventAttribution",
  shadow_trade: "shadowTrade",
  shadow_trade_mark: "shadowTradeMark",
  radar_state_transition: "radarStateTransition",
  conviction_calibration: "convictionCalibration",
  vol_surface_features: "volSurfaceFeatures",
  trade_journal: "tradeJournal",
  option_attribution: "optionAttribution",
  missed_winner_event: "missedWinnerEvent",
  strategy_mutation_proposal: "strategyMutationProposal",
  strategy_backtest_result: "strategyBacktestResult",
  strategy_forward_test_result: "strategyForwardTestResult",
  strategy_cohort_result: "strategyCohortResult",
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
  daily_brief: "dailyBrief",
  feed_signals: "feedSignals",
  universe_screen: "universeScreen",
  watchlist_watched: "watchlistWatched",
  watchlist_unwatched: "watchlistUnwatched",
  watchlist_watched_quotes: "watchlistWatchedQuotes",
  watchlist_unwatched_quotes: "watchlistUnwatchedQuotes",
  watchlist_watched_fundamentals: "watchlistWatchedFundamentals",
  watchlist_unwatched_fundamentals: "watchlistUnwatchedFundamentals",
  watchlist_watched_technicals: "watchlistWatchedTechnicals",
  watchlist_unwatched_technicals: "watchlistUnwatchedTechnicals",
  watchlist_watched_valuations: "watchlistWatchedValuations",
  watchlist_unwatched_valuations: "watchlistUnwatchedValuations",
  watchlist_watched_screener: "watchlistWatchedScreener",
  watchlist_unwatched_screener: "watchlistUnwatchedScreener",
  watchlist_watched_decision_queue: "watchlistWatchedDecisionQueue",
  watchlist_unwatched_decision_queue: "watchlistUnwatchedDecisionQueue",
  watchlist_watched_portfolio: "watchlistWatchedPortfolio",
  watchlist_unwatched_portfolio: "watchlistUnwatchedPortfolio",
  watchlist_watched_options: "watchlistWatchedOptions",
  watchlist_unwatched_options: "watchlistUnwatchedOptions",
  manual_watchlist: "manualWatchlist",
  sources: "sources",
  source_consensus: "sourceConsensus",
  source_ticker_rankings: "sourceTickerRankings",
  source_items: "sourceItems",
  ticker_source_signals: "tickerSourceSignals",
  ownership_consensus: "ownershipConsensus",
  market_context: "marketContext",
  market_valuation_reference_charts: "marketValuationReferenceCharts",
  market_valuation_charts: "marketValuationCharts",
  market_environment_assets: "marketEnvironmentAssets",
  market_environment_model: "marketEnvironmentModel",
  exposure_clusters: "exposureClusters",
  correlation_edges: "correlationEdges",
  portfolio_risk_cards: "portfolioRiskCards",
  review_actions: "reviewActions",
  source_health: "sourceHealth",
  refresh_jobs: "refreshJobs",
};

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
    const dataKey = TABLE_KEYS[apiKey];
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

