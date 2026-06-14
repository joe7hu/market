import type { RowRecord } from "@/types";

type JsonObject = Record<string, unknown>;

export function parseRating(value: string): number {
  const match = value.match(/(\d+(?:\.\d+)?)\s*\/\s*5/);
  return match ? Number(match[1]) : 0;
}

export function parsePercent(value: string): number {
  const match = value.match(/([+-]?\d+(?:\.\d+)?)%/);
  return match ? Number(match[1]) : Number.NaN;
}

export function movingAverageState(price: number, average: number): boolean | null {
  if (!Number.isFinite(price) || !Number.isFinite(average) || average <= 0) return null;
  return price >= average;
}

export function firstFinite(values: number[]): number {
  return values.find((value) => Number.isFinite(value)) ?? Number.NaN;
}

export function safeNumber(value: number, fallback = Number.NEGATIVE_INFINITY): number {
  return Number.isFinite(value) ? value : fallback;
}

export function priceTrendPoints(value: RowRecord[string]): number[] | null {
  if (!Array.isArray(value)) return null;
  const closes = value
    .map((point) => {
      if (typeof point === "number") return point;
      return point && typeof point === "object" && !Array.isArray(point) ? Number(point.close) : Number.NaN;
    })
    .filter((point) => Number.isFinite(point));
  return closes.length >= 2 ? closes : null;
}

export function modeledTrendPoints(basePeriodReturn: number, recentPeriodReturn: number, drawdown: number): number[] {
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

export function oneMonthBars(points: number[]): number[] {
  return periodBars(points, 22);
}

export function periodBars(points: number[], length: number): number[] {
  const period = points.filter((point) => Number.isFinite(point)).slice(-length);
  if (period.length < 2) return [];
  const min = Math.min(...period);
  const max = Math.max(...period);
  const spread = max - min || 1;
  return period.map((point) => ((point - min) / spread) * 100);
}

export function objectField(row: RowRecord | undefined, keys: string[]): JsonObject {
  if (!row) return {};
  for (const key of keys) {
    const value = row[key];
    if (value && typeof value === "object" && !Array.isArray(value)) return value as JsonObject;
  }
  return {};
}

export function objectNumber(object: JsonObject, keys: string[]): number {
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

export function normalizeRatio(value: number): number {
  if (!Number.isFinite(value)) return Number.NaN;
  return Math.abs(value) > 2.5 ? value / 100 : value;
}

export function freeCashFlowYield(freeCashFlow: number, marketCap: number): number {
  return Number.isFinite(freeCashFlow) && Number.isFinite(marketCap) && marketCap > 0 ? freeCashFlow / marketCap : Number.NaN;
}

export function freeCashFlowMargin(freeCashFlow: number, revenue: number): number {
  return Number.isFinite(freeCashFlow) && Number.isFinite(revenue) && revenue > 0 ? freeCashFlow / revenue : Number.NaN;
}

export function inferredFcfMargin(assumptions: JsonObject): number {
  const netMargin = normalizeRatio(objectNumber(assumptions, ["net_margin", "profit_margin"]));
  const conversion = normalizeRatio(objectNumber(assumptions, ["fcf_conversion", "free_cash_flow_conversion"]));
  if (!Number.isFinite(netMargin) || !Number.isFinite(conversion)) return Number.NaN;
  return netMargin * conversion;
}

export function seriesPoints(value: RowRecord[string], keys: string[]): number[] | null {
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

export function normalizedBars(points: number[] | null): number[] {
  if (!points?.length) return [];
  const finite = points.filter((point) => Number.isFinite(point));
  if (finite.length < 2) return [];
  if (finite.every((point) => point >= 0 && point <= 100)) return finite;
  const min = Math.min(...finite);
  const max = Math.max(...finite);
  const spread = max - min || 1;
  return finite.map((point) => ((point - min) / spread) * 100);
}

export function modeledRelativeVolumeBars(relVol: number): number[] {
  if (!Number.isFinite(relVol)) return [];
  return Array.from({ length: 22 }, (_, index) => {
    const wave = Math.sin(index * 0.9) * 0.12;
    const ramp = (index / 21 - 0.5) * Math.min(0.5, Math.abs(relVol - 1));
    return Math.max(0.05, relVol + wave + ramp);
  });
}

export function ratioSeries(points: number[] | null): number[] {
  if (!points?.length) return [];
  return points.map(normalizeRatio).filter((point) => Number.isFinite(point));
}

export function modeledAtrTrend(points: number[]): number[] {
  const recent = points.filter((point) => Number.isFinite(point)).slice(-23);
  if (recent.length < 3) return [];
  const changes = recent.slice(1).map((point, index) => Math.abs(point / recent[index] - 1)).filter((point) => Number.isFinite(point));
  if (changes.length < 2) return [];
  return changes.map((_, index) => {
    const window = changes.slice(Math.max(0, index - 4), index + 1);
    return window.reduce((sum, value) => sum + value, 0) / window.length;
  });
}

export function latestValue(points: number[]): number {
  const value = points.filter((point) => Number.isFinite(point)).at(-1);
  return value ?? Number.NaN;
}

export function closeVolatilityPct(points: number[]): number {
  const modeled = modeledAtrTrend(points);
  return latestValue(modeled);
}

export function expensivenessPercentileFromDiscountHistory(value: RowRecord[string]): number {
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

