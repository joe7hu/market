import type { PanelData, RowRecord } from "@/types";
import { rows } from "@/utils";
import { numberField, textField } from "@/views/rowFormat";

export type WatchlistTab = "active" | "watched" | "owned" | "candidates" | "momentum" | "quality" | "value";
export type WatchlistSort = "rank" | "momentum" | "quality" | "value" | "marketCap" | "drawdown" | "symbol";
export type WatchState = "owned" | "watched" | "candidate";

export type WatchlistFilters = {
  tab: WatchlistTab;
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
  forwardPe: number;
  roic: number;
  rating: number;
  qualityScore: number;
  action: string;
  nextAction: string;
  valueSignal: string;
  valueUpsidePct: number;
  return20d: number;
  return60d: number;
  technicalScore: number;
  drawdownFromHigh: number;
  volumeRatio: number;
  ma20Up: boolean | null;
  ma50Up: boolean | null;
  ma200Up: boolean | null;
  sourceCount: number;
  trend: number[];
};

export type WatchlistViewModel = {
  rows: WatchlistRow[];
  visibleRows: WatchlistRow[];
  counts: Record<WatchlistTab, number>;
  metrics: {
    active: number;
    candidates: number;
    momentumLeaders: number;
    deepDrawdowns: number;
  };
};

export function buildWatchlistViewModel(data: PanelData, filters: WatchlistFilters, localStates: Record<string, WatchState | undefined>): WatchlistViewModel {
  const quoteBySymbol = indexRows(rows(data.quotes));
  const technicalBySymbol = indexRows(rows(data.technicals));
  const valuationBySymbol = latestValuations(rows(data.valuations));
  const allRows = rows(data.universeScreen).map((row) => buildWatchlistRow(row, quoteBySymbol, technicalBySymbol, valuationBySymbol, localStates));
  const rowsWithSymbols = allRows.filter((row) => row.symbol);
  const counts = tabCounts(rowsWithSymbols);
  const visibleRows = sortRows(rowsWithSymbols.filter((row) => filterRow(row, filters)), filters.sort).slice(0, 160);

  return {
    rows: rowsWithSymbols,
    visibleRows,
    counts,
    metrics: {
      active: counts.active,
      candidates: counts.candidates,
      momentumLeaders: rowsWithSymbols.filter((row) => row.technicalScore >= 70).length,
      deepDrawdowns: rowsWithSymbols.filter((row) => row.drawdownFromHigh <= -0.2).length,
    },
  };
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

  return {
    symbol,
    name: textField(row, ["name", "company"], symbol),
    watchState,
    rank: numberField(row, ["rank"], Number.POSITIVE_INFINITY),
    price,
    changePct: firstFinite([numberField(row, ["change_pct"], Number.NaN), numberField(quote, ["change_pct"], Number.NaN)]),
    marketCap: numberField(row, ["market_cap"], Number.NaN),
    forwardPe: numberField(row, ["forward_pe", "pe"], Number.NaN),
    roic: numberField(row, ["roic"], Number.NaN),
    rating: parseRating(textField(row, ["rating"])),
    qualityScore: numberField(row, ["quality_score"], Number.NaN),
    action: textField(row, ["action"], "Watch"),
    nextAction: textField(row, ["next_action"], "Review against evidence before action."),
    valueSignal: textField(row, ["value_signal"], "No valuation row"),
    valueUpsidePct,
    return20d: numberField(technical, ["return_20d"], Number.NaN),
    return60d: numberField(technical, ["return_60d"], Number.NaN),
    technicalScore: numberField(technical, ["technical_score"], Number.NaN),
    drawdownFromHigh: numberField(technical, ["drawdown_from_high"], Number.NaN),
    volumeRatio: numberField(technical, ["volume_ratio_20_60"], Number.NaN),
    ma20Up: movingAverageState(price, numberField(technical, ["ma20"], Number.NaN)),
    ma50Up: movingAverageState(price, numberField(technical, ["ma50"], Number.NaN)),
    ma200Up: movingAverageState(price, numberField(technical, ["ma200"], Number.NaN)),
    sourceCount: numberField(row, ["source_count"], 0),
    trend: trendPoints(numberField(technical, ["return_60d"], 0), numberField(technical, ["return_20d"], 0), numberField(technical, ["drawdown_from_high"], 0)),
  };
}

function filterRow(row: WatchlistRow, filters: WatchlistFilters): boolean {
  const haystack = `${row.symbol} ${row.name} ${row.action} ${row.nextAction}`.toLowerCase();
  if (filters.query.trim() && !haystack.includes(filters.query.trim().toLowerCase())) return false;
  if (filters.minRating && row.rating < filters.minRating) return false;
  if (filters.maxForwardPe !== null && (!Number.isFinite(row.forwardPe) || row.forwardPe > filters.maxForwardPe)) return false;
  if (filters.minRoic !== null && (!Number.isFinite(row.roic) || row.roic < filters.minRoic)) return false;

  if (filters.tab === "active") return row.watchState === "owned" || row.watchState === "watched";
  if (filters.tab === "watched") return row.watchState === "watched";
  if (filters.tab === "owned") return row.watchState === "owned";
  if (filters.tab === "candidates") return row.watchState === "candidate";
  if (filters.tab === "momentum") return row.technicalScore >= 70 || row.return60d >= 0.2;
  if (filters.tab === "quality") return row.rating >= 4 || row.qualityScore >= 70 || row.roic >= 20;
  if (filters.tab === "value") return row.valueUpsidePct >= 15;
  return true;
}

function sortRows(inputRows: WatchlistRow[], sort: WatchlistSort): WatchlistRow[] {
  const sorted = inputRows.slice();
  const numericDesc = (selector: (row: WatchlistRow) => number) => sorted.sort((a, b) => safeNumber(selector(b)) - safeNumber(selector(a)));
  if (sort === "momentum") return numericDesc((row) => row.technicalScore || row.return60d);
  if (sort === "quality") return numericDesc((row) => row.qualityScore);
  if (sort === "value") return numericDesc((row) => row.valueUpsidePct);
  if (sort === "marketCap") return numericDesc((row) => row.marketCap);
  if (sort === "drawdown") return sorted.sort((a, b) => safeNumber(a.drawdownFromHigh) - safeNumber(b.drawdownFromHigh));
  if (sort === "symbol") return sorted.sort((a, b) => a.symbol.localeCompare(b.symbol));
  return sorted.sort((a, b) => safeNumber(a.rank, Number.POSITIVE_INFINITY) - safeNumber(b.rank, Number.POSITIVE_INFINITY));
}

function tabCounts(inputRows: WatchlistRow[]): Record<WatchlistTab, number> {
  return {
    active: inputRows.filter((row) => row.watchState === "owned" || row.watchState === "watched").length,
    watched: inputRows.filter((row) => row.watchState === "watched").length,
    owned: inputRows.filter((row) => row.watchState === "owned").length,
    candidates: inputRows.filter((row) => row.watchState === "candidate").length,
    momentum: inputRows.filter((row) => row.technicalScore >= 70 || row.return60d >= 0.2).length,
    quality: inputRows.filter((row) => row.rating >= 4 || row.qualityScore >= 70 || row.roic >= 20).length,
    value: inputRows.filter((row) => row.valueUpsidePct >= 15).length,
  };
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

function trendPoints(return60d: number, return20d: number, drawdown: number): number[] {
  const baseReturn = Number.isFinite(return60d) ? return60d : 0;
  const recentReturn = Number.isFinite(return20d) ? return20d : baseReturn / 3;
  const pullback = Number.isFinite(drawdown) ? Math.abs(Math.min(0, drawdown)) : 0;
  return Array.from({ length: 12 }, (_, index) => {
    const t = index / 11;
    const drift = baseReturn * t;
    const recent = index > 7 ? recentReturn * (t - 0.65) : 0;
    const wave = Math.sin(index * 1.7) * Math.min(0.04, pullback / 8);
    return 1 + drift + recent + wave;
  });
}
