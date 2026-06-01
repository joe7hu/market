import type { PanelData, RowRecord } from "@/types";
import { rows } from "@/utils";
import { numberField, textField } from "@/views/rowFormat";

export type WatchlistSort = "rank" | "state" | "momentum" | "quality" | "value" | "marketCap" | "drawdown" | "symbol" | "price" | "ps" | "pe" | "forwardPe" | "roic" | "rating" | "returnYtd" | "return1y" | "rsRank1m";
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
  technicalScore: number;
  drawdownFromHigh: number;
  ma20Up: boolean | null;
  ma50Up: boolean | null;
  ma200Up: boolean | null;
  sourceCount: number;
  trend: number[];
  rsRank1m: number;
  rsRankBars: number[];
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
  const allRows = screenRows.map((row) => buildWatchlistRow(row, quoteBySymbol, technicalBySymbol, valuationBySymbol, localStates));
  const rowsWithSymbols = assignRsRanks(allRows.filter((row) => row.symbol));
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

function buildWatchlistRow(row: RowRecord, quoteBySymbol: Map<string, RowRecord>, technicalBySymbol: Map<string, RowRecord>, valuationBySymbol: Map<string, RowRecord>, localStates: Record<string, WatchState | undefined>): WatchlistRow {
  const symbol = textField(row, ["symbol", "ticker"]).toUpperCase();
  const quote = quoteBySymbol.get(symbol);
  const technical = technicalBySymbol.get(symbol);
  const valuation = valuationBySymbol.get(symbol);
  const close = numberField(technical, ["close"], Number.NaN);
  const price = firstFinite([numberField(row, ["price"], Number.NaN), numberField(quote, ["price", "close"], Number.NaN), close]);
  const baseState = normalizeWatchState(textField(row, ["watch_state"], "candidate"));
  const watchState = baseState === "owned" ? "owned" : localStates[symbol] ?? baseState;
  const valueUpsidePct = firstFinite([numberField(valuation, ["upside_pct"], Number.NaN), parsePercent(textField(row, ["value_signal"]))]);

  const return20d = numberField(technical, ["return_20d"], Number.NaN);
  const return60d = numberField(technical, ["return_60d"], Number.NaN);
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

  return {
    symbol,
    name: textField(row, ["name", "company"], symbol),
    watchState,
    rank: numberField(row, ["rank"], Number.POSITIVE_INFINITY),
    price,
    changePct: firstFinite([numberField(row, ["change_pct"], Number.NaN), numberField(quote, ["change_pct"], Number.NaN)]),
    marketCap: numberField(row, ["market_cap"], Number.NaN),
    psRatio: numberField(row, ["ps_ratio"], Number.NaN),
    peRatio: numberField(row, ["pe_ratio", "trailing_pe", "pe"], Number.NaN),
    forwardPe: numberField(row, ["forward_pe"], Number.NaN),
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
    technicalScore: numberField(technical, ["technical_score"], Number.NaN),
    drawdownFromHigh,
    ma20Up,
    ma50Up,
    ma200Up,
    sourceCount: numberField(row, ["source_count"], 0),
    trend,
    rsRank1m: Number.NaN,
    rsRankBars: rsBars,
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

function assignRsRanks(inputRows: WatchlistRow[]): WatchlistRow[] {
  const ranked = inputRows
    .filter((row) => Number.isFinite(row.return20d))
    .sort((a, b) => a.return20d - b.return20d);
  const rankBySymbol = new Map<string, number>();
  ranked.forEach((row, index) => {
    const percentile = ranked.length === 1 ? 100 : 1 + Math.round((index / (ranked.length - 1)) * 98);
    rankBySymbol.set(row.symbol, percentile);
  });
  return inputRows.map((row) => ({ ...row, rsRank1m: rankBySymbol.get(row.symbol) ?? Number.NaN }));
}

function oneMonthBars(points: number[]): number[] {
  const month = points.filter((point) => Number.isFinite(point)).slice(-22);
  if (month.length < 2) return [];
  const min = Math.min(...month);
  const max = Math.max(...month);
  const spread = max - min || 1;
  return month.map((point) => ((point - min) / spread) * 100);
}
