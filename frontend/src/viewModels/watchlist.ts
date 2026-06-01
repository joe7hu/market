import type { PanelData, RowRecord } from "@/types";
import { rows } from "@/utils";
import { numberField, textField } from "@/views/rowFormat";

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
  forwardPe: number;
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
  const fundamentalBySymbol = indexRows([...rows(data.fundamentals), ...rows(data.watchlistWatchedFundamentals), ...rows(data.watchlistUnwatchedFundamentals)]);
  const marketValuationBySymbol = indexRows(rows(data.marketValuationCharts));
  const allRows = screenRows.map((row) => buildWatchlistRow(row, quoteBySymbol, technicalBySymbol, valuationBySymbol, screenerBySymbol, fundamentalBySymbol, marketValuationBySymbol, localStates));
  const rowsWithSymbols = assignRelativeStrengthRanks(allRows.filter((row) => row.symbol));
  const counts = tabCounts(rowsWithSymbols);
  const visibleRows = rowsWithSymbols.filter((row) => filterRow(row, filters));
  const watchedRows = sortRows(visibleRows.filter((row) => isWatched(row)), filters.sort);
  const unwatchedRows = sortRows(visibleRows.filter((row) => !isWatched(row)), filters.sort);
  const totalUnwatchedCount = data.watchlistUnwatched.count ?? counts.unwatched;
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

function buildWatchlistRow(row: RowRecord, quoteBySymbol: Map<string, RowRecord>, technicalBySymbol: Map<string, RowRecord>, valuationBySymbol: Map<string, RowRecord>, screenerBySymbol: Map<string, RowRecord>, fundamentalBySymbol: Map<string, RowRecord>, marketValuationBySymbol: Map<string, RowRecord>, localStates: Record<string, WatchState | undefined>): WatchlistRow {
  const symbol = textField(row, ["symbol", "ticker"]).toUpperCase();
  const quote = quoteBySymbol.get(symbol);
  const technical = technicalBySymbol.get(symbol);
  const valuation = valuationBySymbol.get(symbol);
  const screener = screenerBySymbol.get(symbol);
  const fundamental = fundamentalBySymbol.get(symbol);
  const marketValuation = marketValuationBySymbol.get(symbol);
  const screenerMetrics = objectField(screener, ["metrics"]);
  const fundamentalMetrics = objectField(fundamental, ["metrics"]);
  const valuationAssumptions = objectField(valuation, ["assumptions"]);
  const valuationDiagnostics = objectField(valuation, ["diagnostics"]);
  const close = numberField(technical, ["close"], Number.NaN);
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
  const ma20Up = movingAverageState(price, numberField(technical, ["ma20"], Number.NaN));
  const ma50Up = movingAverageState(price, numberField(technical, ["ma50"], Number.NaN));
  const ma200Up = movingAverageState(price, numberField(technical, ["ma200"], Number.NaN));
  const oneYearTrend = priceTrendPoints(technical?.chart_1y) ?? priceTrendPoints(technical?.price_history_1y);
  const sixtyDayTrend = priceTrendPoints(technical?.price_history_60d);
  const trend = oneYearTrend ?? sixtyDayTrend ?? modeledTrendPoints(return1y, return60d, drawdownFromHigh);
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
    psRatio: numberField(row, ["ps_ratio"], Number.NaN),
    peRatio: numberField(row, ["pe_ratio", "trailing_pe", "pe"], Number.NaN),
    forwardPe: numberField(row, ["forward_pe"], Number.NaN),
    revenueGrowthYoy,
    fcfYield,
    fcfMargin,
    roic: numberField(row, ["roic"], Number.NaN),
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

function normalizeWatchState(value: string): WatchState {
  const normalized = value.toLowerCase();
  if (normalized === "owned") return "owned";
  if (normalized === "watched" || normalized === "watchlist") return "watched";
  return "candidate";
}

function parseRating(value: string): number {
  const match = value.match(/(\d+(?:\.\d+)?)\s*\/\s*5/);
  return match ? Number(match[1]) : 0;
}

function parsePercent(value: string): number {
  const match = value.match(/([+-]?\d+(?:\.\d+)?)%/);
  return match ? Number(match[1]) : Number.NaN;
}

function movingAverageState(price: number, average: number): boolean | null {
  if (!Number.isFinite(price) || !Number.isFinite(average) || average <= 0) return null;
  return price >= average;
}

function firstFinite(values: number[]): number {
  return values.find((value) => Number.isFinite(value)) ?? Number.NaN;
}

function safeNumber(value: number, fallback = Number.NEGATIVE_INFINITY): number {
  return Number.isFinite(value) ? value : fallback;
}

function stateRank(state: WatchState): number {
  if (state === "owned") return 0;
  if (state === "watched") return 1;
  return 2;
}

function priceTrendPoints(value: RowRecord[string]): number[] | null {
  if (!Array.isArray(value)) return null;
  const closes = value
    .map((point) => {
      if (typeof point === "number") return point;
      return point && typeof point === "object" && !Array.isArray(point) ? Number(point.close) : Number.NaN;
    })
    .filter((point) => Number.isFinite(point));
  return closes.length >= 2 ? closes : null;
}

function modeledTrendPoints(basePeriodReturn: number, recentPeriodReturn: number, drawdown: number): number[] {
  const baseReturn = Number.isFinite(basePeriodReturn) ? basePeriodReturn : 0;
  const recentReturn = Number.isFinite(recentPeriodReturn) ? recentPeriodReturn : baseReturn / 3;
  const pullback = Number.isFinite(drawdown) ? Math.abs(Math.min(0, drawdown)) : 0;
  return Array.from({ length: 64 }, (_, index) => {
    const t = index / 63;
    const drift = baseReturn * t;
    const recent = index > 42 ? recentReturn * (t - 0.65) : 0;
    const wave = Math.sin(index * 1.7) * Math.min(0.04, pullback / 8);
    return 1 + drift + recent + wave;
  });
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

function oneMonthBars(points: number[]): number[] {
  return periodBars(points, 22);
}

function periodBars(points: number[], length: number): number[] {
  const period = points.filter((point) => Number.isFinite(point)).slice(-length);
  if (period.length < 2) return [];
  const min = Math.min(...period);
  const max = Math.max(...period);
  const spread = max - min || 1;
  return period.map((point) => ((point - min) / spread) * 100);
}

type JsonObject = Record<string, unknown>;

function objectField(row: RowRecord | undefined, keys: string[]): JsonObject {
  if (!row) return {};
  for (const key of keys) {
    const value = row[key];
    if (value && typeof value === "object" && !Array.isArray(value)) return value as JsonObject;
  }
  return {};
}

function objectNumber(object: JsonObject, keys: string[]): number {
  for (const key of keys) {
    const parsed = numberFromUnknown(object[key]);
    if (Number.isFinite(parsed)) return parsed;
  }
  return Number.NaN;
}

function numberFromUnknown(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value.trim().replace(/[$,%_,]/g, ""));
    if (Number.isFinite(parsed)) return parsed;
  }
  return Number.NaN;
}

function normalizeRatio(value: number): number {
  if (!Number.isFinite(value)) return Number.NaN;
  return Math.abs(value) > 2.5 ? value / 100 : value;
}

function freeCashFlowYield(freeCashFlow: number, marketCap: number): number {
  return Number.isFinite(freeCashFlow) && Number.isFinite(marketCap) && marketCap > 0 ? freeCashFlow / marketCap : Number.NaN;
}

function freeCashFlowMargin(freeCashFlow: number, revenue: number): number {
  return Number.isFinite(freeCashFlow) && Number.isFinite(revenue) && revenue > 0 ? freeCashFlow / revenue : Number.NaN;
}

function inferredFcfMargin(assumptions: JsonObject): number {
  const netMargin = normalizeRatio(objectNumber(assumptions, ["net_margin", "profit_margin"]));
  const conversion = normalizeRatio(objectNumber(assumptions, ["fcf_conversion", "free_cash_flow_conversion"]));
  if (!Number.isFinite(netMargin) || !Number.isFinite(conversion)) return Number.NaN;
  return netMargin * conversion;
}

function seriesPoints(value: RowRecord[string], keys: string[]): number[] | null {
  if (!Array.isArray(value)) return null;
  const points = value
    .map((point) => {
      if (typeof point === "number") return point;
      if (!point || typeof point !== "object" || Array.isArray(point)) return Number.NaN;
      for (const key of keys) {
        const parsed = numberFromUnknown((point as JsonObject)[key]);
        if (Number.isFinite(parsed)) return parsed;
      }
      return Number.NaN;
    })
    .filter((point) => Number.isFinite(point));
  return points.length >= 2 ? points : null;
}

function normalizedBars(points: number[] | null): number[] {
  if (!points?.length) return [];
  const finite = points.filter((point) => Number.isFinite(point));
  if (finite.length < 2) return [];
  if (finite.every((point) => point >= 0 && point <= 100)) return finite;
  const min = Math.min(...finite);
  const max = Math.max(...finite);
  const spread = max - min || 1;
  return finite.map((point) => ((point - min) / spread) * 100);
}

function modeledRelativeVolumeBars(relVol: number): number[] {
  if (!Number.isFinite(relVol)) return [];
  return Array.from({ length: 22 }, (_, index) => {
    const wave = Math.sin(index * 0.9) * 0.12;
    const ramp = (index / 21 - 0.5) * Math.min(0.5, Math.abs(relVol - 1));
    return Math.max(0.05, relVol + wave + ramp);
  });
}

function ratioSeries(points: number[] | null): number[] {
  if (!points?.length) return [];
  return points.map(normalizeRatio).filter((point) => Number.isFinite(point));
}

function modeledAtrTrend(points: number[]): number[] {
  const recent = points.filter((point) => Number.isFinite(point)).slice(-23);
  if (recent.length < 3) return [];
  const changes = recent.slice(1).map((point, index) => Math.abs(point / recent[index] - 1)).filter((point) => Number.isFinite(point));
  if (changes.length < 2) return [];
  return changes.map((_, index) => {
    const window = changes.slice(Math.max(0, index - 4), index + 1);
    return window.reduce((sum, value) => sum + value, 0) / window.length;
  });
}

function latestValue(points: number[]): number {
  const value = points.filter((point) => Number.isFinite(point)).at(-1);
  return value ?? Number.NaN;
}

function closeVolatilityPct(points: number[]): number {
  const modeled = modeledAtrTrend(points);
  return latestValue(modeled);
}

function expensivenessPercentileFromDiscountHistory(value: RowRecord[string]): number {
  if (!Array.isArray(value)) return Number.NaN;
  const values = value
    .map((point) => point && typeof point === "object" && !Array.isArray(point) ? numberFromUnknown((point as JsonObject).discount_pct) : Number.NaN)
    .filter((point) => Number.isFinite(point));
  const current = values.at(-1) ?? Number.NaN;
  if (!Number.isFinite(current) || values.length < 2) return Number.NaN;
  const sorted = values.slice().sort((a, b) => a - b);
  const below = sorted.filter((point) => point < current).length;
  return 100 - (below / (sorted.length - 1)) * 100;
}
