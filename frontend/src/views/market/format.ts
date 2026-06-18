import type { LineData, PriceFormat, Time } from "lightweight-charts";

import type { JsonValue, RowRecord } from "@/types";
import { formatPct, numberField, textField } from "@/views/rowFormat";

import type { MetricPoint } from "./types";

export function featuredAssetRows(inputRows: RowRecord[]): RowRecord[] {
  const groupLimits: Record<string, number> = {
    Market: 12,
    Macro: 8,
    Sectors: 12,
    Industries: 12,
    "Managed ETFs": 8,
    Countries: 8,
    Others: 8,
  };
  return Object.entries(groupLimits)
    .flatMap(([group, limit]) => inputRows.filter((row) => textField(row, ["group_name"]) === group).slice(0, limit))
    .slice(0, 48);
}

export function latestAssetMatrixDate(rows: RowRecord[]): string {
  return rows
    .map((row) => textField(row, ["as_of", "date"]).slice(0, 10))
    .filter(Boolean)
    .sort()
    .at(-1) ?? "";
}

export function assetRowClass(rows: RowRecord[], row: RowRecord, index: number): string {
  const group = textField(row, ["group_name"]);
  const previous = index > 0 ? textField(rows[index - 1], ["group_name"]) : "";
  const separator = index > 0 && group !== previous ? "border-t-2 border-t-border" : "";
  return `border-b border-border align-middle transition-colors hover:bg-accent/45 ${separator} ${groupBackgroundClass(group)}`;
}

export function returnBarStyle(value: number, maxAbs: number): { left?: string; right?: string; width: string } {
  const width = `${Math.max(3, Math.min(50, (Math.abs(value) / maxAbs) * 50))}%`;
  return value >= 0 ? { left: "50%", width } : { right: "50%", width };
}

export function returnTextClass(value: number): string {
  if (!Number.isFinite(value)) return "text-muted-foreground";
  if (value > 0) return "text-green-700";
  if (value < 0) return "text-red-700";
  return "text-muted-foreground";
}

export function metricHistoryPoints(row: RowRecord): MetricPoint[] {
  const value = row.history;
  if (!Array.isArray(value)) return [];
  const points = value
    .filter((point): point is Record<string, JsonValue> => typeof point === "object" && point !== null && !Array.isArray(point))
    .map((point) => ({
      date: typeof point.date === "string" ? point.date.slice(0, 10) : "",
      value: numeric(point.value),
      index_price: numeric(point.index_price),
    }))
    .filter((point) => point.date && Number.isFinite(point.value));
  const stride = Math.max(1, Math.ceil(points.length / 420));
  return points.filter((_, index) => index % stride === 0 || index === points.length - 1);
}

export function filterMetricPeriod(points: MetricPoint[], years: number): MetricPoint[] {
  if (!years) return points;
  const cutoff = new Date();
  cutoff.setFullYear(cutoff.getFullYear() - years);
  const cutoffDate = cutoff.toISOString().slice(0, 10);
  return points.filter((point) => point.date >= cutoffDate);
}

export function metricLineData(points: MetricPoint[], key: "value" | "index_price"): LineData<Time>[] {
  return points
    .filter((point) => typeof point[key] === "number" && Number.isFinite(point[key]) && (key !== "index_price" || Number(point[key]) > 0))
    .map((point) => ({ time: point.date as Time, value: Number(point[key]) }));
}

export function metricPriceFormat(suffix: string): PriceFormat {
  if (suffix === "%") {
    return {
      type: "custom",
      formatter: (value) => `${Number(value).toFixed(2)}%`,
      minMove: 0.01,
    };
  }
  if (suffix === "x") {
    return {
      type: "custom",
      formatter: (value) => `${Number(value).toFixed(2)}x`,
      minMove: 0.01,
    };
  }
  return {
    type: "price",
    precision: 2,
    minMove: 0.01,
  };
}

export function isMarketDriver(category: string): boolean {
  return ["Valuation", "Price Trend", "Market Breadth", "Risk Appetite", "Sector / Theme Leadership"].includes(category);
}

export function weightedDriverScore(inputRows: RowRecord[]): number {
  let weighted = 0;
  let total = 0;
  for (const row of inputRows) {
    const score = numberField(row, ["score"], Number.NaN);
    const weight = numberField(row, ["weight"], Number.NaN);
    if (!Number.isFinite(score) || !Number.isFinite(weight) || weight <= 0) continue;
    weighted += score * weight;
    total += weight;
  }
  return total > 0 ? weighted / total : Number.NaN;
}

export function postureFromScore(value: number): string {
  if (!Number.isFinite(value)) return "not enough data";
  if (value >= 70) return "constructive";
  if (value >= 45) return "mixed";
  return "defensive";
}

export function numeric(value: JsonValue | undefined): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value !== "string") return undefined;
  const parsed = Number(value.replace(/[$,%_,]/g, ""));
  return Number.isFinite(parsed) ? parsed : undefined;
}

export function titleCase(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function formatMaybePct(value: number): string {
  return Number.isFinite(value) ? formatPct(value) : "-";
}

export function formatMetricValue(value: number, suffix: string): string {
  if (!Number.isFinite(value)) return "-";
  if (suffix === "%") return `${value.toFixed(2)}%`;
  if (suffix === "x") return `${value.toFixed(2)}x`;
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

export function formatScore(value: number): string {
  return Number.isFinite(value) ? `${Math.round(value)} / 100` : "-";
}

export function percentileTone(row: RowRecord, percentile: number): string {
  const higherIsBetter = row.higher_is_better === true;
  const good = higherIsBetter ? percentile >= 70 : percentile <= 30;
  const bad = higherIsBetter ? percentile <= 30 : percentile >= 70;
  if (good) return "ml-2 font-medium text-green-700";
  if (bad) return "ml-2 font-medium text-red-700";
  return "ml-2 font-medium text-amber-700";
}

export function returnToneClass(value: number): string {
  if (!Number.isFinite(value)) return "bg-muted text-muted-foreground";
  if (value >= 10) return "bg-green-100 text-green-900";
  if (value > 0) return "bg-green-50 text-green-800";
  if (value <= -10) return "bg-red-100 text-red-900";
  if (value < 0) return "bg-red-50 text-red-800";
  return "bg-muted text-muted-foreground";
}

export function groupBackgroundClass(group: string): string {
  if (group === "Market") return "bg-blue-50/30";
  if (group === "Macro") return "bg-amber-50/35";
  if (group === "Sectors") return "bg-green-50/25";
  if (group === "Industries") return "bg-violet-50/25";
  if (group === "Managed ETFs") return "bg-sky-50/25";
  if (group === "Countries") return "bg-cyan-50/25";
  return "bg-background";
}

export function groupPillClass(group: string): string {
  if (group === "Market") return "border-blue-200 bg-blue-50 text-blue-800";
  if (group === "Macro") return "border-amber-200 bg-amber-50 text-amber-800";
  if (group === "Sectors") return "border-green-200 bg-green-50 text-green-800";
  if (group === "Industries") return "border-violet-200 bg-violet-50 text-violet-800";
  if (group === "Managed ETFs") return "border-sky-200 bg-sky-50 text-sky-800";
  if (group === "Countries") return "border-cyan-200 bg-cyan-50 text-cyan-800";
  return "border-border bg-muted text-muted-foreground";
}

export function postureBadge(value: string): "default" | "secondary" | "outline" | "destructive" | "success" | "warning" | "info" {
  const normalized = value.toLowerCase();
  if (normalized.includes("constructive") || normalized.includes("discounted") || normalized.includes("attractive")) return "success";
  if (normalized.includes("defensive") || normalized.includes("stretched")) return "warning";
  if (normalized.includes("not enough") || normalized.includes("missing")) return "outline";
  return "info";
}

export function scoreColor(value: number): string {
  if (!Number.isFinite(value)) return "var(--muted-foreground)";
  if (value >= 70) return "var(--success)";
  if (value >= 45) return "var(--primary)";
  return "var(--warning)";
}

export function normalizeScore(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}
