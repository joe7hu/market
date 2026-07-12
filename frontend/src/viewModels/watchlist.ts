import type { PanelData, RowRecord } from "@/types";
import { rows } from "@/utils";
import { numberField, textField } from "@/views/rowFormat";
import { closeVolatilityPct, expensivenessPercentileFromDiscountHistory, firstFinite, freeCashFlowMargin, freeCashFlowYield, inferredFcfMargin, latestValue, modeledAtrTrend, modeledRelativeVolumeBars, movingAverageState, normalizedBars, normalizeRatio, objectField, objectNumber, oneMonthBars, parsePercent, parseRating, periodBars, priceTrendPoints, ratioSeries, safeNumber, seriesPoints } from "./watchlistMath";

export type WatchlistSort = "rank" | "state" | "momentum" | "quality" | "value" | "marketCap" | "drawdown" | "symbol" | "price" | "ps" | "pe" | "forwardPe" | "roic" | "rating" | "returnYtd" | "return1y" | "rsRank1m" | "rsRank3m" | "revenueGrowth" | "fcfYield" | "fcfMargin" | "relVol1m" | "atrPct1m" | "valuationPercentile";
export type WatchState = "owned" | "watched" | "candidate";

export type WatchlistFilters = {
  query: string;
  minRating: number;
  maxForwardPe: number | null;
  minRoic: number | null;
  sort: WatchlistSort;
};

export type WatchlistRow = {
  symbol: string;
  name: string;
  watchState: WatchState;
  rank: number;
  price: number;
  changePct: number;
  marketCap: number;
  psRatio: number;
  peRatio: number;
  peStatus: string;
  forwardPe: number;
  forwardPeStatus: string;
  revenueGrowthYoy: number;
  fcfYield: number;
  fcfMargin: number;
  roic: number;
  rating: number;
  qualityScore: number;
  action: string;
  nextAction: string;
  valueSignal: string;
  valueUpsidePct: number;
  returnYtd: number;
  return1y: number;
  return20d: number;
  return60d: number;
  return3m: number;
  technicalScore: number;
  drawdownFromHigh: number;
  ma20Up: boolean | null;
  ma50Up: boolean | null;
  ma200Up: boolean | null;
  sourceCount: number;
  trend: number[];
  rsRank1m: number;
  rsRank3m: number;
  rsRankBars: number[];
  rsRank3mBars: number[];
  relVol1m: number;
  relVolBars: number[];
  atrPct1m: number;
  atrTrend: number[];
  valuationPercentile: number;
  optionsStatus: string;
  optionsIvRegime: string;
  optionsExpectedMovePct: number;
  optionsSkewSignal: string;
  optionsSpreadQuality: string;
  researchStatus: "review" | "packet" | "memo" | "none";
  researchLabel: string;
  researchDetail: string;
  researchEvidenceCount: number;
};

export type WatchlistViewModel = {
  rows: WatchlistRow[];
  visibleRows: WatchlistRow[];
  watchedRows: WatchlistRow[];
  unwatchedRows: WatchlistRow[];
  counts: {
    watched: number;
    owned: number;
    unwatched: number;
    momentum: number;
    quality: number;
    value: number;
  };
  metrics: {
    active: number;
    candidates: number;
    momentumLeaders: number;
    deepDrawdowns: number;
  };
};

export function buildWatchlistViewModel(data: PanelData, filters: WatchlistFilters, localStates: Record<string, WatchState | undefined>): WatchlistViewModel {
  const sectionRows = [...rows(data.watchlistWatched), ...rows(data.watchlistUnwatched)];
  const screenRows = sectionRows.length ? sectionRows : rows(data.universeScreen);
  const quoteBySymbol = indexRows([...rows(data.quotes), ...rows(data.watchlistWatchedQuotes), ...rows(data.watchlistUnwatchedQuotes)]);
  const technicalBySymbol = indexRows([...rows(data.technicals), ...rows(data.watchlistWatchedTechnicals), ...rows(data.watchlistUnwatchedTechnicals)]);
  const valuationBySymbol = latestValuations([...rows(data.valuations), ...rows(data.watchlistWatchedValuations), ...rows(data.watchlistUnwatchedValuations)]);
  const screenerBySymbol = indexRows([...rows(data.screener), ...rows(data.watchlistWatchedScreener), ...rows(data.watchlistUnwatchedScreener)]);
  const fundamentalBySymbol = preferredFundamentals([...rows(data.fundamentals), ...rows(data.watchlistWatchedFundamentals), ...rows(data.watchlistUnwatchedFundamentals)]);
  const marketValuationBySymbol = indexRows(rows(data.marketValuationCharts));
  const optionsBySymbol = indexRows([...rows(data.optionsTickerSignals), ...rows(data.watchlistWatchedOptions), ...rows(data.watchlistUnwatchedOptions)]);
  const researchPacketBySymbol = latestRows([...rows(data.researchPackets), ...rows(data.watchlistWatchedResearchPackets), ...rows(data.watchlistUnwatchedResearchPackets)]);
  const memoBySymbol = latestRows([...rows(data.memos), ...rows(data.watchlistWatchedMemos), ...rows(data.watchlistUnwatchedMemos)]);
  const thesisBySymbol = latestRows([...rows(data.thesisMonitor), ...rows(data.watchlistWatchedThesisMonitor), ...rows(data.watchlistUnwatchedThesisMonitor)]);
  const allRows = screenRows.map((row) => buildWatchlistRow(row, quoteBySymbol, technicalBySymbol, valuationBySymbol, screenerBySymbol, fundamentalBySymbol, marketValuationBySymbol, optionsBySymbol, researchPacketBySymbol, memoBySymbol, thesisBySymbol, localStates));
  const rowsWithSymbols = assignRelativeStrengthRanks(allRows.filter((row) => row.symbol));
  const counts = tabCounts(rowsWithSymbols);
  const visibleRows = rowsWithSymbols.filter((row) => filterRow(row, filters));
  const watchedRows = sortRows(visibleRows.filter((row) => isWatched(row)), filters.sort);
  const unwatchedRows = sortRows(visibleRows.filter((row) => !isWatched(row)), filters.sort);
  const totalUnwatchedCount = data.watchlistUnwatched?.count ?? counts.unwatched;
  const displayCounts = filtersAreActive(filters) ? counts : { ...counts, unwatched: Math.max(counts.unwatched, totalUnwatchedCount) };

  return {
    rows: rowsWithSymbols,
    visibleRows: [...watchedRows, ...unwatchedRows],
    watchedRows,
    unwatchedRows,
    counts: displayCounts,
    metrics: {
      active: displayCounts.watched + displayCounts.owned,
      candidates: displayCounts.unwatched,
      momentumLeaders: rowsWithSymbols.filter((row) => row.technicalScore >= 70).length,
      deepDrawdowns: rowsWithSymbols.filter((row) => row.drawdownFromHigh <= -0.2).length,
    },
  };
}

function filtersAreActive(filters: WatchlistFilters): boolean {
  return Boolean(filters.query.trim() || filters.minRating || filters.maxForwardPe !== null || filters.minRoic !== null);
}

function buildWatchlistRow(row: RowRecord, quoteBySymbol: Map<string, RowRecord>, technicalBySymbol: Map<string, RowRecord>, valuationBySymbol: Map<string, RowRecord>, screenerBySymbol: Map<string, RowRecord>, fundamentalBySymbol: Map<string, RowRecord>, marketValuationBySymbol: Map<string, RowRecord>, optionsBySymbol: Map<string, RowRecord>, researchPacketBySymbol: Map<string, RowRecord>, memoBySymbol: Map<string, RowRecord>, thesisBySymbol: Map<string, RowRecord>, localStates: Record<string, WatchState | undefined>): WatchlistRow {
  const symbol = textField(row, ["symbol", "ticker"]).toUpperCase();
  const quote = quoteBySymbol.get(symbol);
  const technical = technicalBySymbol.get(symbol);
  const valuation = valuationBySymbol.get(symbol);
  const screener = screenerBySymbol.get(symbol);
  const fundamental = fundamentalBySymbol.get(symbol);
  const marketValuation = marketValuationBySymbol.get(symbol);
  const options = optionsBySymbol.get(symbol);
  const researchPacket = researchPacketBySymbol.get(symbol);
  const memo = memoBySymbol.get(symbol);
  const thesis = thesisBySymbol.get(symbol);
  const screenerMetrics = objectField(screener, ["metrics", "values"]);
  const fundamentalMetrics = objectField(fundamental, ["metrics", "values"]);
  const valuationAssumptions = objectField(valuation, ["assumptions"]);
  const valuationDiagnostics = objectField(valuation, ["diagnostics"]);
  const close = numberField(technical, ["close", "price"], Number.NaN);
  const price = firstFinite([numberField(row, ["price"], Number.NaN), numberField(quote, ["price", "close"], Number.NaN), close]);
  const baseState = normalizeWatchState(textField(row, ["watch_state"], "candidate"));
  const watchState = baseState === "owned" ? "owned" : localStates[symbol] ?? baseState;
  const valueUpsidePct = firstFinite([numberField(valuation, ["upside_pct"], Number.NaN), parsePercent(textField(row, ["value_signal"]))]);
  const marketCap = firstFinite([numberField(row, ["market_cap"], Number.NaN), objectNumber(screenerMetrics, ["market_cap", "marketCap", "market_cap_basic", "market_capitalization"]), objectNumber(valuationAssumptions, ["market_cap"])]);
  const revenue = firstFinite([objectNumber(fundamentalMetrics, ["revenue", "total_revenue", "totalRevenue"]), objectNumber(screenerMetrics, ["total_revenue", "totalRevenue", "revenue"]), objectNumber(valuationAssumptions, ["revenue"])]);
  const freeCashFlow = firstFinite([numberField(row, ["free_cash_flow", "freeCashFlow", "fcf"], Number.NaN), objectNumber(fundamentalMetrics, ["free_cash_flow", "freeCashFlow", "free_cashflow", "fcf"]), objectNumber(screenerMetrics, ["free_cash_flow", "freeCashFlow", "freeCashflow", "free_cashflow", "fcf"])]);
  const revenueGrowthYoy = normalizeRatio(firstFinite([numberField(row, ["revenue_growth_yoy", "revenue_yoy", "revenue_growth"], Number.NaN), objectNumber(fundamentalMetrics, ["revenue_growth", "revenueGrowth", "revenue_growth_yoy", "revenue_yoy"]), objectNumber(screenerMetrics, ["revenue_growth", "revenueGrowth", "revenue_growth_yoy", "revenue_yoy"]), objectNumber(valuationAssumptions, ["revenue_growth"])]));
  const fcfYield = normalizeRatio(firstFinite([numberField(row, ["fcf_yield", "free_cash_flow_yield", "freeCashFlowYield"], Number.NaN), objectNumber(fundamentalMetrics, ["fcf_yield", "free_cash_flow_yield", "freeCashFlowYield"]), objectNumber(screenerMetrics, ["fcf_yield", "free_cash_flow_yield", "freeCashFlowYield"]), freeCashFlowYield(freeCashFlow, marketCap)]));
  const fcfMargin = normalizeRatio(firstFinite([numberField(row, ["fcf_margin", "free_cash_flow_margin", "freeCashFlowMargin"], Number.NaN), objectNumber(fundamentalMetrics, ["fcf_margin", "free_cash_flow_margin", "freeCashFlowMargin"]), objectNumber(screenerMetrics, ["fcf_margin", "free_cash_flow_margin", "freeCashFlowMargin"]), freeCashFlowMargin(freeCashFlow, revenue), inferredFcfMargin(valuationAssumptions)]));

  const return20d = numberField(technical, ["return_20d"], Number.NaN);
  const return60d = numberField(technical, ["return_60d"], Number.NaN);
  const return3m = firstFinite([numberField(technical, ["return_3m", "rs_3m"], Number.NaN), numberField(row, ["rs_3m", "return_3m"], Number.NaN), return60d]);
  const returnYtd = numberField(technical, ["return_ytd"], Number.NaN);
  const return1y = numberField(technical, ["return_1y"], Number.NaN);
  const drawdownFromHigh = numberField(technical, ["drawdown_from_high"], Number.NaN);
  const ma20Up = movingAverageState(price, numberField(technical, ["ma20", "sma_20"], Number.NaN));
  const ma50Up = movingAverageState(price, numberField(technical, ["ma50", "sma_50"], Number.NaN));
  const ma200Up = movingAverageState(price, numberField(technical, ["ma200", "sma_200"], Number.NaN));
  const oneYearTrend = priceTrendPoints(technical?.chart_1y) ?? priceTrendPoints(technical?.price_history_1y);
  const sixtyDayTrend = priceTrendPoints(technical?.price_history_60d);
  const trend = oneYearTrend ?? sixtyDayTrend ?? [];
  const rsBars = priceTrendPoints(technical?.rs_1m_bars) ?? oneMonthBars(trend);
  const rs3mBars = priceTrendPoints(technical?.rs_3m_bars) ?? periodBars(trend, 63);
  const relVol1m = firstFinite([numberField(technical, ["relative_volume_1m", "rel_vol_1m", "rel_volume_1m", "volume_ratio_20_60", "volume_ratio"], Number.NaN), objectNumber(screenerMetrics, ["relative_volume_1m", "rel_vol_1m", "rel_volume_1m", "volume_ratio"])]);
  const relVolBars = normalizedBars(seriesPoints(technical?.relative_volume_1m_bars, ["value", "relative_volume", "rel_volume", "volume"]) ?? seriesPoints(technical?.rel_vol_1m_bars, ["value", "relative_volume", "rel_volume", "volume"]) ?? seriesPoints(technical?.volume_bars_1m, ["value", "volume"]) ?? seriesPoints(technical?.volume_1m_bars, ["value", "volume"]) ?? modeledRelativeVolumeBars(relVol1m));
  const atrTrend = ratioSeries(seriesPoints(technical?.atr_pct_1m_points, ["value", "atr_pct", "atr_percent"]) ?? seriesPoints(technical?.atr_pct_1m_bars, ["value", "atr_pct", "atr_percent"]) ?? modeledAtrTrend(trend));
  const atrPct1m = normalizeRatio(firstFinite([numberField(technical, ["atr_pct_1m", "atr_1m_pct", "atr_pct", "atr_percent", "average_true_range_pct"], Number.NaN), latestValue(atrTrend), closeVolatilityPct(trend)]));
  const valuationPercentile = firstFinite([numberField(row, ["valuation_percentile_own_history", "own_history_percentile", "valuation_percentile", "valuation_pct_rank", "percentile"], Number.NaN), numberField(valuation, ["valuation_percentile_own_history", "own_history_percentile", "valuation_percentile", "percentile"], Number.NaN), objectNumber(valuationDiagnostics, ["valuation_percentile", "percentile"]), numberField(marketValuation, ["valuation_percentile", "percentile"], Number.NaN), expensivenessPercentileFromDiscountHistory(marketValuation?.history)]);

  return {
    symbol,
    name: textField(row, ["name", "company"], symbol),
    watchState,
    rank: numberField(row, ["rank"], Number.POSITIVE_INFINITY),
    price,
    changePct: firstFinite([numberField(row, ["change_pct"], Number.NaN), numberField(quote, ["change_pct"], Number.NaN)]),
    marketCap,
    psRatio: firstFinite([numberField(row, ["ps_ratio"], Number.NaN), objectNumber(fundamentalMetrics, ["ps_ratio", "price_to_sales"])]),
    peRatio: firstFinite([numberField(row, ["pe_ratio", "trailing_pe", "pe"], Number.NaN), objectNumber(fundamentalMetrics, ["pe_ratio", "trailing_pe", "pe"])]),
    peStatus: textField(row, ["pe_status"], "missing"),
    forwardPe: firstFinite([numberField(row, ["forward_pe"], Number.NaN), objectNumber(fundamentalMetrics, ["forward_pe", "forwardPe"])]),
    forwardPeStatus: textField(row, ["forward_pe_status"], "missing"),
    revenueGrowthYoy,
    fcfYield,
    fcfMargin,
    roic: firstFinite([numberField(row, ["roic"], Number.NaN), objectNumber(fundamentalMetrics, ["roic", "return_on_invested_capital"])]),
    rating: parseRating(textField(row, ["rating"])),
    qualityScore: numberField(row, ["quality_score"], Number.NaN),
    action: textField(row, ["action"], "Watch"),
    nextAction: textField(row, ["next_action"], "Review against evidence before action."),
    valueSignal: textField(row, ["value_signal"], "No valuation row"),
    valueUpsidePct,
    returnYtd,
    return1y,
    return20d,
    return60d,
    return3m,
    technicalScore: numberField(technical, ["technical_score"], Number.NaN),
    drawdownFromHigh,
    ma20Up,
    ma50Up,
    ma200Up,
    sourceCount: numberField(row, ["source_count"], 0),
    trend,
    rsRank1m: firstFinite([numberField(row, ["rs_rank_1m"], Number.NaN), numberField(technical, ["rs_rank_1m"], Number.NaN)]),
    rsRank3m: firstFinite([numberField(row, ["rs_rank_3m", "rs_3m"], Number.NaN), numberField(technical, ["rs_rank_3m"], Number.NaN)]),
    rsRankBars: rsBars,
    rsRank3mBars: rs3mBars,
    relVol1m,
    relVolBars,
    atrPct1m,
    atrTrend,
    valuationPercentile,
    optionsStatus: textField(options, ["status", "state"], "missing"),
    optionsIvRegime: textField(options, ["iv_regime"], "unknown"),
    optionsExpectedMovePct: numberField(options, ["expected_move_pct"], Number.NaN),
    optionsSkewSignal: textField(options, ["skew_signal"], "unknown"),
    optionsSpreadQuality: textField(options, ["spread_quality"], "unknown"),
    ...researchSignal(researchPacket, memo, thesis),
  };
}

function researchSignal(researchPacket: RowRecord | undefined, memo: RowRecord | undefined, thesis: RowRecord | undefined): Pick<WatchlistRow, "researchStatus" | "researchLabel" | "researchDetail" | "researchEvidenceCount"> {
  const needsReview = Boolean(thesis?.needs_review);
  const evidenceCount = firstFinite([numberField(researchPacket, ["evidence_count"], Number.NaN), numberField(thesis, ["evidence_count"], Number.NaN), 0]);
  if (needsReview) {
    return {
      researchStatus: "review",
      researchLabel: "Review",
      researchDetail: textField(thesis, ["review_reason"], "Thesis needs review."),
      researchEvidenceCount: evidenceCount,
    };
  }
  if (memo) {
    return {
      researchStatus: "memo",
      researchLabel: "Memo",
      researchDetail: textField(memo, ["report_type"], "Decision memo available."),
      researchEvidenceCount: evidenceCount,
    };
  }
  if (researchPacket) {
    return {
      researchStatus: "packet",
      researchLabel: textField(researchPacket, ["conviction", "decision"], "Packet"),
      researchDetail: textField(researchPacket, ["decision"], "Research packet ready."),
      researchEvidenceCount: evidenceCount,
    };
  }
  return {
    researchStatus: "none",
    researchLabel: "None",
    researchDetail: "No packet or thesis review row loaded.",
    researchEvidenceCount: 0,
  };
}

function filterRow(row: WatchlistRow, filters: WatchlistFilters): boolean {
  const haystack = `${row.symbol} ${row.name} ${row.action} ${row.nextAction}`.toLowerCase();
  if (filters.query.trim() && !haystack.includes(filters.query.trim().toLowerCase())) return false;
  if (filters.minRating && row.rating < filters.minRating) return false;
  if (filters.maxForwardPe !== null && (!Number.isFinite(row.forwardPe) || row.forwardPe > filters.maxForwardPe)) return false;
  if (filters.minRoic !== null && (!Number.isFinite(row.roic) || row.roic < filters.minRoic)) return false;

  return true;
}

function sortRows(inputRows: WatchlistRow[], sort: WatchlistSort): WatchlistRow[] {
  const sorted = inputRows.slice();
  const numericDesc = (selector: (row: WatchlistRow) => number) => sorted.sort((a, b) => safeNumber(selector(b)) - safeNumber(selector(a)));
  const numericAsc = (selector: (row: WatchlistRow) => number) => sorted.sort((a, b) => safeNumber(selector(a), Number.POSITIVE_INFINITY) - safeNumber(selector(b), Number.POSITIVE_INFINITY));
  if (sort === "state") return sorted.sort((a, b) => stateRank(a.watchState) - stateRank(b.watchState) || a.symbol.localeCompare(b.symbol));
  if (sort === "momentum") return numericDesc((row) => row.technicalScore || row.return60d);
  if (sort === "quality") return numericDesc((row) => row.qualityScore);
  if (sort === "value") return numericDesc((row) => row.valueUpsidePct);
  if (sort === "marketCap") return numericDesc((row) => row.marketCap);
  if (sort === "drawdown") return sorted.sort((a, b) => safeNumber(a.drawdownFromHigh) - safeNumber(b.drawdownFromHigh));
  if (sort === "symbol") return sorted.sort((a, b) => a.symbol.localeCompare(b.symbol));
  if (sort === "price") return numericDesc((row) => row.price);
  if (sort === "ps") return numericAsc((row) => row.psRatio);
  if (sort === "pe") return numericAsc((row) => row.peRatio);
  if (sort === "forwardPe") return numericAsc((row) => row.forwardPe);
  if (sort === "roic") return numericDesc((row) => row.roic);
  if (sort === "rating") return numericDesc((row) => row.rating);
  if (sort === "returnYtd") return numericDesc((row) => row.returnYtd);
  if (sort === "return1y") return numericDesc((row) => row.return1y);
  if (sort === "rsRank1m") return numericDesc((row) => row.rsRank1m);
  if (sort === "rsRank3m") return numericDesc((row) => row.rsRank3m);
  if (sort === "revenueGrowth") return numericDesc((row) => row.revenueGrowthYoy);
  if (sort === "fcfYield") return numericDesc((row) => row.fcfYield);
  if (sort === "fcfMargin") return numericDesc((row) => row.fcfMargin);
  if (sort === "relVol1m") return numericDesc((row) => row.relVol1m);
  if (sort === "atrPct1m") return numericAsc((row) => row.atrPct1m);
  if (sort === "valuationPercentile") return numericAsc((row) => row.valuationPercentile);
  return sorted.sort((a, b) => safeNumber(a.rank, Number.POSITIVE_INFINITY) - safeNumber(b.rank, Number.POSITIVE_INFINITY));
}

function tabCounts(inputRows: WatchlistRow[]): WatchlistViewModel["counts"] {
  return {
    watched: inputRows.filter((row) => row.watchState === "watched").length,
    owned: inputRows.filter((row) => row.watchState === "owned").length,
    unwatched: inputRows.filter((row) => !isWatched(row)).length,
    momentum: inputRows.filter((row) => row.technicalScore >= 70 || row.return60d >= 0.2).length,
    quality: inputRows.filter((row) => row.rating >= 4 || row.qualityScore >= 70 || row.roic >= 20).length,
    value: inputRows.filter((row) => row.valueUpsidePct >= 15).length,
  };
}

function isWatched(row: WatchlistRow): boolean {
  return row.watchState === "owned" || row.watchState === "watched";
}

function indexRows(inputRows: RowRecord[]): Map<string, RowRecord> {
  const indexed = new Map<string, RowRecord>();
  for (const row of inputRows) {
    const symbol = textField(row, ["symbol", "ticker"]).toUpperCase();
    if (symbol && !indexed.has(symbol)) indexed.set(symbol, row);
  }
  return indexed;
}

function preferredFundamentals(inputRows: RowRecord[]): Map<string, RowRecord> {
  const indexed = new Map<string, RowRecord>();
  for (const row of inputRows) {
    const symbol = textField(row, ["symbol", "ticker"]).toUpperCase();
    if (!symbol) continue;
    const existing = indexed.get(symbol);
    const priority = fundamentalPriority(textField(row, ["metric_set"]));
    const existingPriority = existing ? fundamentalPriority(textField(existing, ["metric_set"])) : -1;
    const observedAt = textField(row, ["observed_at", "period_end"]);
    const existingObservedAt = existing ? textField(existing, ["observed_at", "period_end"]) : "";
    if (!existing || priority > existingPriority || (priority === existingPriority && observedAt > existingObservedAt)) {
      indexed.set(symbol, row);
    }
  }
  return indexed;
}

function fundamentalPriority(metricSet: string): number {
  if (metricSet === "sec_fundamentals") return 3;
  if (metricSet === "company_fundamentals" || metricSet === "fundamentals") return 2;
  if (metricSet === "analyst_estimates" || metricSet === "consensus") return 1;
  return 0;
}

function latestValuations(inputRows: RowRecord[]): Map<string, RowRecord> {
  const bySymbol = new Map<string, RowRecord>();
  for (const row of inputRows) {
    const symbol = textField(row, ["symbol", "ticker"]).toUpperCase();
    const existing = bySymbol.get(symbol);
    if (!symbol) continue;
    if (!existing || textField(row, ["method"]).includes("blended")) bySymbol.set(symbol, row);
  }
  return bySymbol;
}

function latestRows(inputRows: RowRecord[]): Map<string, RowRecord> {
  const bySymbol = new Map<string, RowRecord>();
  for (const row of inputRows) {
    const symbol = textField(row, ["symbol", "ticker"]).toUpperCase();
    if (!symbol) continue;
    const existing = bySymbol.get(symbol);
    if (!existing || textField(row, ["created_at", "updated_at", "as_of", "last_reviewed"]) > textField(existing, ["created_at", "updated_at", "as_of", "last_reviewed"])) {
      bySymbol.set(symbol, row);
    }
  }
  return bySymbol;
}

function normalizeWatchState(value: string): WatchState {
  const normalized = value.toLowerCase();
  if (normalized === "owned") return "owned";
  if (normalized === "watched" || normalized === "watchlist") return "watched";
  return "candidate";
}

function stateRank(state: WatchState): number {
  if (state === "owned") return 0;
  if (state === "watched") return 1;
  return 2;
}

function assignRelativeStrengthRanks(inputRows: WatchlistRow[]): WatchlistRow[] {
  const rowsMissing1m = inputRows.filter((row) => !Number.isFinite(row.rsRank1m));
  const rowsMissing3m = inputRows.filter((row) => !Number.isFinite(row.rsRank3m));
  const rs1mBySymbol = relativeStrengthRank(rowsMissing1m, (row) => row.return20d);
  const rs3mBySymbol = relativeStrengthRank(rowsMissing3m, (row) => firstFinite([row.return3m, row.return60d, row.return20d]));
  return inputRows.map((row) => ({
    ...row,
    rsRank1m: Number.isFinite(row.rsRank1m) ? row.rsRank1m : rs1mBySymbol.get(row.symbol) ?? Number.NaN,
    rsRank3m: Number.isFinite(row.rsRank3m) ? row.rsRank3m : rs3mBySymbol.get(row.symbol) ?? Number.NaN,
  }));
}

function relativeStrengthRank(inputRows: WatchlistRow[], selector: (row: WatchlistRow) => number): Map<string, number> {
  const ranked = inputRows
    .filter((row) => Number.isFinite(selector(row)))
    .sort((a, b) => selector(a) - selector(b));
  const rankBySymbol = new Map<string, number>();
  ranked.forEach((row, index) => {
    const percentile = ranked.length === 1 ? 100 : 1 + Math.round((index / (ranked.length - 1)) * 98);
    rankBySymbol.set(row.symbol, percentile);
  });
  return rankBySymbol;
}
